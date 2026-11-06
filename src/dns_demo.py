"""教材原理复现：真实 DNS 查询与报文解码演示。

负责人：组员B（D4）
参考：docs/specs/project-roadmap.md 阶段2 D4、报告第一部分素材

目的：
- 用 dnspython 向配置的递归解析器查询少量已知正常域名
- 记录请求/响应并逐字段解码（Header/Question/Answer/NS/Additional）
- 明确区分 stub→递归解析器 与 递归解析器→根/TLD/权威 的区别
- 不将普通 Resolver 调用当成逐级迭代演示，不声称看到了解析器内部查询

使用：
  python -m src.dns_demo                    # 实际查询
  python -m src.dns_demo --dry-run          # 仅打印将要查询的域名和 nameserver
  python -m src.dns_demo --nameserver 8.8.8.8  # 显式指定 nameserver

网络查询仅手动、少量、可复现；常规 pytest 不依赖外网。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

import dns.flags
import dns.rcode
import dns.resolver


DEFAULT_DOMAINS: list[tuple[str, str]] = [
    ("www.baidu.com", "A"),
    ("www.qq.com", "A"),
    ("www.example.com", "A"),
    ("mail.cctv.com", "A"),
    ("github.com", "A"),
    ("this-domain-should-not-exist-xyz12345.example", "A"),
]

RCODE_NAMES: dict[int, str] = {
    0: "NOERROR",
    1: "FORMERR",
    2: "SERVFAIL",
    3: "NXDOMAIN",
    4: "NOTIMP",
    5: "REFUSED",
}


@dataclass
class SectionRecord:
    name: str
    rtype: str
    ttl: Optional[int]
    rdata: str


@dataclass
class QueryResult:
    domain: str
    qtype: str
    nameservers: list[str]
    success: bool
    error: Optional[str] = None
    duration_ms: float = 0.0

    id: Optional[int] = None
    qr: Optional[str] = None
    opcode: Optional[str] = None
    aa: Optional[bool] = None
    tc: Optional[bool] = None
    rd: Optional[bool] = None
    ra: Optional[bool] = None
    rcode: Optional[str] = None

    question: list[dict] = field(default_factory=list)
    answer: list[SectionRecord] = field(default_factory=list)
    authority: list[SectionRecord] = field(default_factory=list)
    additional: list[SectionRecord] = field(default_factory=list)

    raw_response_b64: Optional[str] = None


def get_system_nameservers() -> list[str]:
    resolver = dns.resolver.Resolver()
    return list(resolver.nameservers)


def build_resolver(nameserver_override: Optional[str] = None) -> dns.resolver.Resolver:
    resolver = dns.resolver.Resolver()
    if nameserver_override:
        resolver.nameservers = [nameserver_override]
    resolver.lifetime = 5.0
    resolver.timeout = 3.0
    return resolver


def decode_flags(flags) -> dict:
    f = int(flags)
    return {
        "QR": bool(f & 0x8000),
        "OPCODE": (f >> 11) & 0xF,
        "AA": bool(f & 0x0400),
        "TC": bool(f & 0x0200),
        "RD": bool(f & 0x0100),
        "RA": bool(f & 0x0080),
    }


def rtype_name(rdtype: int) -> str:
    try:
        import dns.rdatatype
        return dns.rdatatype.to_text(rdtype)
    except Exception:
        return f"TYPE{rdtype}"


def rclass_name(rclass: int) -> str:
    try:
        import dns.rdataclass
        return dns.rdataclass.to_text(rclass)
    except Exception:
        return f"CLASS{rclass}"


def _decode_rdata(rrset) -> str:
    parts = []
    for rdata in rrset:
        parts.append(str(rdata))
    return ", ".join(parts)


def make_section_records(rrset_list) -> list[SectionRecord]:
    records = []
    for rrset in rrset_list:
        for rdata in rrset:
            records.append(SectionRecord(
                name=str(rrset.name),
                rtype=rtype_name(rrset.rdtype),
                ttl=rrset.ttl,
                rdata=str(rdata),
            ))
    return records


def _fill_response_fields(result: QueryResult, response) -> None:
    result.id = response.id
    flags = response.flags
    fd = decode_flags(flags)
    result.qr = "response" if fd["QR"] else "query"
    result.opcode = ["QUERY", "IQUERY", "STATUS", None, "NOTIFY", "UPDATE"][fd["OPCODE"]] if fd["OPCODE"] < 6 else str(fd["OPCODE"])
    result.aa = fd["AA"]
    result.tc = fd["TC"]
    result.rd = fd["RD"]
    result.ra = fd["RA"]
    rc = int(response.rcode())
    result.rcode = RCODE_NAMES.get(rc, f"RCODE{rc}")

    result.question = [
        {"name": str(q.name), "type": rtype_name(q.rdtype), "class": rclass_name(q.rdclass)}
        for q in response.question
    ]
    result.answer = make_section_records(response.answer)
    result.authority = make_section_records(response.authority)
    result.additional = make_section_records(response.additional)

    try:
        result.raw_response_b64 = base64.b64encode(response.to_wire()).decode("ascii")
    except Exception:
        result.raw_response_b64 = None


def perform_query(domain: str, qtype: str, resolver: dns.resolver.Resolver) -> QueryResult:
    result = QueryResult(
        domain=domain,
        qtype=qtype,
        nameservers=list(resolver.nameservers),
        success=False,
    )

    start = time.monotonic()
    try:
        answer = resolver.resolve(domain, qtype)
        result.success = True
        _fill_response_fields(result, answer.response)

    except dns.resolver.NXDOMAIN as e:
        result.success = True
        try:
            qnames_list = list(e.qnames()) if callable(e.qnames) else list(e.qnames)
            if qnames_list:
                response = e.response(qnames_list[0])
                _fill_response_fields(result, response)
        except Exception:
            pass
        result.error = "NXDOMAIN"

    except dns.resolver.NoAnswer as e:
        result.success = True
        try:
            qnames_list = list(e.qnames()) if callable(e.qnames) else list(e.qnames)
            if qnames_list:
                response = e.response(qnames_list[0])
                _fill_response_fields(result, response)
        except Exception:
            pass
        result.error = "NO_ANSWER"

    except dns.resolver.Timeout:
        result.error = "TIMEOUT"
    except dns.resolver.NoNameservers:
        result.error = "NO_NAMESERVERS"
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"

    result.duration_ms = (time.monotonic() - start) * 1000
    return result


def print_result(r: QueryResult) -> None:
    print("=" * 72)
    print(f"Query : {r.domain}  type={r.qtype}")
    print(f"NS    : {', '.join(r.nameservers)}")
    print(f"Result: {'OK' if r.success else 'FAIL'}  ({r.duration_ms:.1f} ms)  RCODE={r.rcode}")
    if r.error and not r.success:
        print(f"Error : {r.error}")

    if r.id is not None:
        print(f"\n--- DNS Header ---")
        print(f"  ID={r.id}  QR={r.qr}  OPCODE={r.opcode}")
        print(f"  AA={r.aa}  TC={r.tc}  RD={r.rd}  RA={r.ra}")
        print(f"  RCODE={r.rcode}")
        print(f"  RD=1 是 stub 向递归解析器发递归查询的标志（教材第 267 页）")
        print(f"  RA=1 表示递归解析器支持递归（本地服务器代你去迭代查根/TLD/权威）")

    if r.question:
        print(f"\n--- Question ({len(r.question)} entries) ---")
        for q in r.question:
            print(f"  {q['name']}  {q['type']}  {q['class']}")

    if r.answer:
        print(f"\n--- Answer ({len(r.answer)} entries) ---")
        for a in r.answer:
            print(f"  {a.name}  {a.rtype}  TTL={a.ttl}s  {a.rdata}")
    else:
        print(f"\n--- Answer (empty) ---")

    if r.authority:
        print(f"\n--- Authority ({len(r.authority)} entries) ---")
        print(f"  注意：Authority 节出现的 NS 记录是递归解析器返回给 stub 的，")
        print(f"  不是我们观测到的根→TLD→权威的内部迭代查询。")
        for a in r.authority:
            print(f"  {a.name}  {a.rtype}  TTL={a.ttl}s  {a.rdata}")

    if r.additional:
        print(f"\n--- Additional ({len(r.additional)} entries) ---")
        for a in r.additional:
            print(f"  {a.name}  {a.rtype}  TTL={a.ttl}s  {a.rdata}")

    if r.rcode == "NXDOMAIN":
        print(f"\n  NXDOMAIN 说明递归解析器从权威服务器得到了该域名不存在的确认。")
        print(f"  这是 DGA 可能伴随大量 NXDOMAIN 的原因之一（路线图 2.3 节），")
        print(f"  但当前三特征方案未把 NXDOMAIN 当作检测特征。")


def save_json(results: list[QueryResult], out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = os.path.join(out_dir, f"dns_demo_{ts}.json")

    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "nameservers_observed": list(results[0].nameservers) if results else [],
        "stub_resolver_note": (
            "这些是 stub resolver 向配置的递归解析器发送递归查询后，"
            "递归解析器返回的完整响应。我们只看到了 stub 与递归解析器的一次交互，"
            "看不到递归解析器内部向根/TLD/权威服务器做的迭代查询。"
        ),
        "results": [asdict(r) for r in results],
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    return path


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DNS 教材原理复现演示")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅打印将要查询的域名和 nameserver，不实际发送请求")
    parser.add_argument("--nameserver", type=str, default=None,
                        help="显式指定 nameserver，覆盖系统默认")
    parser.add_argument("--save", action="store_true",
                        help="将结果保存到 report/evidence/dns_demo_*.json")
    parser.add_argument("--output-dir", type=str, default="report/evidence",
                        help="JSON 输出目录（默认 report/evidence）")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    system_ns = get_system_nameservers()
    ns_display = args.nameserver or system_ns

    print("=" * 72)
    print("DNS 教材原理复现 — D4 演示")
    print("=" * 72)
    print(f"系统默认 nameserver: {', '.join(system_ns)}")
    print(f"本次使用 nameserver : {', '.join([ns_display] if isinstance(ns_display, str) else ns_display)}")
    print()
    print("说明：")
    print("  1. dnspython Resolver 发的是递归查询（RD=1），只和递归解析器交互一次。")
    print("  2. 递归解析器内部向根→TLD→权威服务器做的迭代查询，我们看不到。")
    print("  3. 响应中的 Authority/Additional 节只是递归解析器告诉我们下一步可以向谁查，")
    print("     不是我们亲自观测到的迭代链路。")
    print()

    if args.dry_run:
        print("--- DRY RUN ---")
        print(f"将查询 {len(DEFAULT_DOMAINS)} 个域名：")
        for d, qt in DEFAULT_DOMAINS:
            print(f"  {qt:>5}  {d}")
        print(f"\nnameserver: {args.nameserver or '系统默认'}")
        print("未发送任何网络请求。")
        return 0

    resolver = build_resolver(args.nameserver)
    results = []

    for domain, qtype in DEFAULT_DOMAINS:
        r = perform_query(domain, qtype, resolver)
        results.append(r)
        print_result(r)

    if args.save:
        path = save_json(results, args.output_dir)
        print(f"\n结果已保存: {path}")

    ok = sum(1 for r in results if r.success)
    print(f"\n--- 汇总: {ok}/{len(results)} 成功 ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())

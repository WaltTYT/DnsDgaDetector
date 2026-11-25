"""特征计算：域名熵值、窗口频率、查询间隔。

负责人：组员B（D5）
参考：docs/specs/project-roadmap.md 2.2 评估单元、2.3 特征表、
      docs/plans/data-contracts.md v1 第 4 节

计算三项特征：
- H  香农熵（bit/字符），输入为可注册域标签（PSL 公共后缀左侧第一个 label）
- F  同客户端 60 秒窗口总查询次数（窗口级，所有 qname 合计）
- I  同客户端窗口内相邻查询时间差中位数，<3 次查询记 NaN

聚合键：(client_ip, window_start, qname)，window_start 为 UTC 整分钟起点。

关键约束：
- tldextract 必须离线（禁用缓存和联网更新 PSL）
- F 和 I 是 (client_ip, window_start) 粒度，同窗口所有聚合样本继承相同值
- label/source/algorithm/split 只用于基线样本筛选和评估，不进入特征计算
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import tldextract


# dns_log.csv 的列顺序契约，校验时要求列名与顺序完全一致
DNS_LOG_COLUMNS = [
    "event_id", "timestamp", "client_ip", "qname", "qtype",
    "label", "source", "algorithm", "split",
]

# results/features.csv 的列顺序契约：前 7 列为聚合键与元数据，其余为特征、规则结论与 LLM 复核结果
FEATURES_COLUMNS = [
    "client_ip", "window_start", "qname",
    "label", "source", "algorithm", "split",
    "H", "F", "I", "E", "B", "R",
    "llm_verdict", "llm_fail_reason", "hybrid",
]

# 粗略匹配 IPv4 字面量（不校验各段是否 <=255），命中则不做熵特征
IP_LITERAL_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
# 国际化域名编码前缀，出现即视为不适合做字符熵的样本
PUNYCODE_PREFIX = "xn--"

# UTC 纪元基准时间，用作时间解析的统一下界（当前实现未直接引用）
_ABSOLUTE_MIN = pd.Timestamp("1970-01-01T00:00:00Z")


# 本地 PSL 快照，固定离线读取，禁止运行时联网更新后缀列表
_PSL_PATH = Path(__file__).resolve().parents[1] / "data" / "raw" / "public_suffix_list.dat"


@lru_cache(maxsize=1)
def _make_tldextract() -> tldextract.TLDExtract:
    # 进程内只构造一次解析器：只读本地快照，禁用下载缓存与联网回退
    if not _PSL_PATH.is_file():
        # 快照缺失时直接报错，避免 tldextract 隐式去下载 PSL
        raise FileNotFoundError(f"PSL snapshot not found: {_PSL_PATH}")
    return tldextract.TLDExtract(
        include_psl_private_domains=True,
        cache_dir=None,
        suffix_list_urls=(_PSL_PATH.as_uri(),),
        fallback_to_snapshot=False,
    )


def normalize_qname(qname: str) -> str:
    # 统一域名写法：去首尾空白、转小写、去掉末尾根点；非字符串输入返回空串
    if not isinstance(qname, str):
        return ""
    qname = qname.strip().lower()
    if qname.endswith("."):
        # 只去掉一个末尾根点，把绝对域名写法与相对写法归一
        qname = qname[:-1]
    return qname


def extract_label(qname: str,
                  ext: Optional[tldextract.TLDExtract] = None) -> tuple[str, str]:
    # 取 PSL 公共后缀左侧第一个标签；返回 (标签, 状态)，状态用于统计被跳过的原因
    if not qname:
        return "", "empty"
    if IP_LITERAL_RE.match(qname):
        # IP 字面量不是域名，不参与熵计算
        return "", "ip_literal"
    if PUNYCODE_PREFIX in qname:
        # punycode 编码后的字符熵没有可比性，直接跳过
        return "", "punycode"

    if ext is None:
        # 允许调用方复用解析器，未传入时使用带缓存的全局实例
        ext = _make_tldextract()

    result = ext(qname)
    if not result.domain:
        return "", "no_domain"
    return result.domain, "ok"


def shannon_entropy(s: str) -> float:
    # 字符级香农熵（bit/字符）：H = -Σ p·log2(p)，空串返回 NaN，结果保留 4 位小数
    if not s:
        return float("nan")
    n = len(s)
    # 先统计每个字符出现次数，再换算成概率
    counts: dict[str, int] = {}
    for c in s:
        counts[c] = counts.get(c, 0) + 1
    h = 0.0
    for count in counts.values():
        p = count / n
        h -= p * math.log2(p)
    return round(h, 4)


def align_to_window_start(ts_iso: str) -> str:
    # 把时间戳归一到 UTC 整分钟窗口起点；无时区的按 UTC 解释，避免本地时区偏移
    ts = pd.Timestamp(ts_iso)
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    # 向下取整到分钟，保证同一窗口内的查询落到同一个 key
    floored = ts.floor("1min")
    return floored.strftime("%Y-%m-%dT%H:%M:00Z")


def compute_features(log_df: pd.DataFrame) -> pd.DataFrame:
    # 主流程：校验日志 → 归一 qname → 对齐 60 秒窗口 → 逐条算 H → 聚合窗口级 F/I
    _validate_log_columns(log_df)

    # 在副本上派生中间列，不修改调用方传入的日志表
    df = log_df.copy()
    df["qname_norm"] = df["qname"].apply(normalize_qname)
    df["window_start"] = df["timestamp"].apply(align_to_window_start)

    # 整批共用同一个解析器实例，避免重复加载 PSL
    ext = _make_tldextract()
    labels = df["qname_norm"].apply(lambda q: extract_label(q, ext))
    df["label_psl"] = labels.apply(lambda t: t[0])
    # label_status 只用于排查（empty/ip_literal/punycode/no_domain），不进入特征
    df["label_status"] = labels.apply(lambda t: t[1])

    # 熵按可注册域标签计算；被跳过的样本标签为空，熵为 NaN
    df["H"] = df["label_psl"].apply(shannon_entropy)

    # F 为窗口级特征：同一 (client_ip, window_start) 内所有 qname 的记录数合计
    window_stats = (
        df.groupby(["client_ip", "window_start"])
          .agg(
              F=("event_id", "size"),
              _first_ts=("timestamp", "first"),
          )
          .reset_index()
    )

    def _compute_interval(group: pd.DataFrame) -> Optional[float]:
        # 窗口级间隔特征 I：组内相邻查询时间差的中位数（秒），不足 3 次查询记 NaN
        if len(group) < 3:
            return float("nan")
        # 统一按 UTC 解析并排序，只对相邻差值求中位数
        ts = pd.to_datetime(group["timestamp"], utc=True).sort_values()
        diffs = ts.diff().dropna().dt.total_seconds()
        if len(diffs) == 0:
            # 理论上 len>=3 时至少有两个差值，这里兜底异常数据
            return float("nan")
        return round(float(np.median(diffs.to_numpy())), 3)

    # 同样按客户端+窗口粒度计算 I，再与 F 合并成窗口级统计表
    intervals = (
        df.groupby(["client_ip", "window_start"], group_keys=False)
          .apply(_compute_interval)
          .rename("I")
          .reset_index()
    )
    window_stats = window_stats.merge(
        intervals, on=["client_ip", "window_start"], how="left"
    )

    # 聚合到最终单元 (client_ip, window_start, qname)：元数据取窗口内首条记录
    agg_df = df.groupby(
        ["client_ip", "window_start", "qname_norm"], as_index=False
    ).first()

    # 用归一后的 qname 替换原始写法，保证同一域名的大小写/根点差异被合并
    agg_df = agg_df.drop(columns=["qname"], errors="ignore")
    agg_df = agg_df.rename(columns={"qname_norm": "qname"})

    # F/I 是窗口级特征，同窗口内所有聚合样本继承相同值
    agg_df = agg_df.merge(
        window_stats[["client_ip", "window_start", "F", "I"]],
        on=["client_ip", "window_start"],
        how="left",
    )

    meta_cols = ["client_ip", "window_start", "qname",
                 "label", "source", "algorithm", "split"]
    feat_cols = ["H", "F", "I"]
    # 按契约顺序先取元数据与三项特征，判定列由下游模块后续填充
    out = agg_df[meta_cols + feat_cols].copy()

    # 规则与 LLM 结果先置空，等 baseline/rule_engine/llm_judge 阶段写入
    out["E"] = pd.NA
    out["B"] = pd.NA
    out["R"] = pd.NA
    out["llm_verdict"] = pd.NA
    out["llm_fail_reason"] = pd.NA
    out["hybrid"] = pd.NA

    # 统一列顺序，便于 save_features 的契约校验
    out = out[FEATURES_COLUMNS]

    return out


def load_log(path: str) -> pd.DataFrame:
    # 读取日志；client_ip/qname 强制为字符串，避免 IP 被推断成数值或丢失前导零
    df = pd.read_csv(path, dtype={"client_ip": str, "qname": str})
    _validate_log_columns(df)
    return df


def save_features(df: pd.DataFrame, path: str) -> None:
    # 落盘前校验列契约，index=False 保证不写入多余的行号列
    _validate_features_columns(df)
    df.to_csv(path, index=False)


def _validate_log_columns(df: pd.DataFrame) -> None:
    # 校验日志列名与顺序，再检查 split/label 取值集合，任何越界立即失败
    actual = list(df.columns)
    expected = DNS_LOG_COLUMNS
    if actual != expected:
        raise ValueError(
            f"dns_log.csv columns mismatch.\n"
            f"  expected: {expected}\n"
            f"  actual:   {actual}"
        )
    # 划分取值必须可枚举，避免拼写错误让样本意外绕过隔离
    valid_splits = {"train", "val", "test"}
    if not df["split"].isin(valid_splits).all():
        bad = df.loc[~df["split"].isin(valid_splits), "split"].unique().tolist()
        raise ValueError(f"Invalid split values: {bad}")
    # 标签只允许 0/1，防止标签编码不一致导致后续判定失真
    if not df["label"].isin([0, 1]).all():
        bad = df.loc[~df["label"].isin([0, 1]), "label"].unique().tolist()
        raise ValueError(f"Invalid label values: {bad}")


def _validate_features_columns(df: pd.DataFrame) -> None:
    # 特征表列名与顺序必须与契约完全一致，否则拒绝输出
    actual = list(df.columns)
    if actual != FEATURES_COLUMNS:
        raise ValueError(
            f"features.csv columns mismatch.\n"
            f"  expected: {FEATURES_COLUMNS}\n"
            f"  actual:   {actual}"
        )

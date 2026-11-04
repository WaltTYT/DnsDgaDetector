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
from typing import Optional

import numpy as np
import pandas as pd
import tldextract


DNS_LOG_COLUMNS = [
    "event_id", "timestamp", "client_ip", "qname", "qtype",
    "label", "source", "algorithm", "split",
]

FEATURES_COLUMNS = [
    "client_ip", "window_start", "qname",
    "label", "source", "algorithm", "split",
    "H", "F", "I", "E", "B", "R",
    "llm_verdict", "llm_fail_reason", "hybrid",
]

IP_LITERAL_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
PUNYCODE_PREFIX = "xn--"

_ABSOLUTE_MIN = pd.Timestamp("1970-01-01T00:00:00Z")


def _make_tldextract() -> tldextract.TLDExtract:
    return tldextract.TLDExtract(
        include_psl_private_domains=False,
        cache_dir=None,
        fallback_to_snapshot=False,
    )


def normalize_qname(qname: str) -> str:
    if not isinstance(qname, str):
        return ""
    qname = qname.strip().lower()
    if qname.endswith("."):
        qname = qname[:-1]
    return qname


def extract_label(qname: str,
                  ext: Optional[tldextract.TLDExtract] = None) -> tuple[str, str]:
    if not qname:
        return "", "empty"
    if IP_LITERAL_RE.match(qname):
        return "", "ip_literal"
    if PUNYCODE_PREFIX in qname:
        return "", "punycode"

    if ext is None:
        ext = _make_tldextract()

    result = ext(qname)
    if not result.domain:
        return "", "no_domain"
    return result.domain, "ok"


def shannon_entropy(s: str) -> float:
    if not s:
        return float("nan")
    n = len(s)
    counts: dict[str, int] = {}
    for c in s:
        counts[c] = counts.get(c, 0) + 1
    h = 0.0
    for count in counts.values():
        p = count / n
        h -= p * math.log2(p)
    return round(h, 4)


def align_to_window_start(ts_iso: str) -> str:
    ts = pd.Timestamp(ts_iso)
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    floored = ts.floor("1min")
    return floored.strftime("%Y-%m-%dT%H:%M:00Z")


def compute_features(log_df: pd.DataFrame) -> pd.DataFrame:
    _validate_log_columns(log_df)

    df = log_df.copy()
    df["qname_norm"] = df["qname"].apply(normalize_qname)
    df["window_start"] = df["timestamp"].apply(align_to_window_start)

    ext = _make_tldextract()
    labels = df["qname_norm"].apply(lambda q: extract_label(q, ext))
    df["label_psl"] = labels.apply(lambda t: t[0])
    df["label_status"] = labels.apply(lambda t: t[1])

    df["H"] = df["label_psl"].apply(shannon_entropy)

    window_stats = (
        df.groupby(["client_ip", "window_start"])
          .agg(
              F=("event_id", "size"),
              _first_ts=("timestamp", "first"),
          )
          .reset_index()
    )

    def _compute_interval(group: pd.DataFrame) -> Optional[float]:
        if len(group) < 3:
            return float("nan")
        ts = pd.to_datetime(group["timestamp"], utc=True).sort_values()
        diffs = ts.diff().dropna().dt.total_seconds()
        if len(diffs) == 0:
            return float("nan")
        return round(float(np.median(diffs.to_numpy())), 3)

    intervals = (
        df.groupby(["client_ip", "window_start"], group_keys=False)
          .apply(_compute_interval)
          .rename("I")
          .reset_index()
    )
    window_stats = window_stats.merge(
        intervals, on=["client_ip", "window_start"], how="left"
    )

    agg_df = df.groupby(
        ["client_ip", "window_start", "qname_norm"], as_index=False
    ).first()

    agg_df = agg_df.drop(columns=["qname"], errors="ignore")
    agg_df = agg_df.rename(columns={"qname_norm": "qname"})

    agg_df = agg_df.merge(
        window_stats[["client_ip", "window_start", "F", "I"]],
        on=["client_ip", "window_start"],
        how="left",
    )

    meta_cols = ["client_ip", "window_start", "qname",
                 "label", "source", "algorithm", "split"]
    feat_cols = ["H", "F", "I"]
    out = agg_df[meta_cols + feat_cols].copy()

    out["E"] = pd.NA
    out["B"] = pd.NA
    out["R"] = pd.NA
    out["llm_verdict"] = pd.NA
    out["llm_fail_reason"] = pd.NA
    out["hybrid"] = pd.NA

    out = out[FEATURES_COLUMNS]

    return out


def load_log(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"client_ip": str, "qname": str})
    _validate_log_columns(df)
    return df


def save_features(df: pd.DataFrame, path: str) -> None:
    _validate_features_columns(df)
    df.to_csv(path, index=False)


def _validate_log_columns(df: pd.DataFrame) -> None:
    actual = list(df.columns)
    expected = DNS_LOG_COLUMNS
    if actual != expected:
        raise ValueError(
            f"dns_log.csv columns mismatch.\n"
            f"  expected: {expected}\n"
            f"  actual:   {actual}"
        )
    valid_splits = {"train", "val", "test"}
    if not df["split"].isin(valid_splits).all():
        bad = df.loc[~df["split"].isin(valid_splits), "split"].unique().tolist()
        raise ValueError(f"Invalid split values: {bad}")
    if not df["label"].isin([0, 1]).all():
        bad = df.loc[~df["label"].isin([0, 1]), "label"].unique().tolist()
        raise ValueError(f"Invalid label values: {bad}")


def _validate_features_columns(df: pd.DataFrame) -> None:
    actual = list(df.columns)
    if actual != FEATURES_COLUMNS:
        raise ValueError(
            f"features.csv columns mismatch.\n"
            f"  expected: {FEATURES_COLUMNS}\n"
            f"  actual:   {actual}"
        )

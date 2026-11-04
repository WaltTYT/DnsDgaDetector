"""基线拟合：仅用训练集正常数据统计阈值。

负责人：组员B（D5）
参考：docs/specs/project-roadmap.md 2.3 基线与规则、
      docs/plans/d4-d6-design.md §2.3

核心约束：
- 只读取 split=train 且 label=0 的数据，禁止触碰验证/测试集
- H  阈值取训练集正常唯一可注册域标签熵分布的 P99，严格比较 >
- F  阈值取训练集正常窗口级频率分布的 P99，严格比较 >
- I  阈值取训练集正常窗口级间隔分布（≥3 次查询的窗口）的 P1，严格比较 <
- 记录分位数实现、样本量、缺失数量、阈值；P1=0 时标注 i_rule_potentially_ineffective
- 输出 results/baseline.json 和 results/figures/baseline_histograms.png
"""

from __future__ import annotations

import json
import os
import platform
import sys
from datetime import datetime, timezone
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from features import (
    FEATURES_COLUMNS,
    _make_tldextract,
    extract_label,
    normalize_qname,
    shannon_entropy,
)


BASELINE_SCHEMA_VERSION = 1

BASELINE_REQUIRED_KEYS = {
    "version", "window_seconds", "fitted_on", "fitted_at",
    "python_version", "numpy_version", "pandas_version", "psl_source",
    "features", "freeze_check",
}


def _validate_features_input(df: pd.DataFrame) -> None:
    actual = list(df.columns)
    if actual != FEATURES_COLUMNS:
        raise ValueError(
            f"features input columns mismatch.\n"
            f"  expected: {FEATURES_COLUMNS}\n"
            f"  actual:   {actual}"
        )
    if not df["split"].isin(["train", "val", "test"]).all():
        raise ValueError("Invalid split values in features input")


def _filter_train_normal(log_df: pd.DataFrame) -> pd.DataFrame:
    train = log_df[log_df["split"] == "train"].copy()
    train_normal = train[train["label"] == 0].copy()
    if train_normal.empty:
        raise ValueError("No train+label=0 samples available for baseline fitting")
    n_total = len(log_df)
    n_non_train = n_total - len(train)
    n_non_normal_in_train = len(train) - len(train_normal)
    if n_non_train > 0 or n_non_normal_in_train > 0:
        pass
    return train_normal


def _build_h_distribution(train_normal: pd.DataFrame) -> np.ndarray:
    ext = _make_tldextract()
    train_normal_local = train_normal.copy()

    train_normal_local["qname_norm"] = train_normal_local["qname"].apply(normalize_qname)
    labels = train_normal_local["qname_norm"].apply(lambda q: extract_label(q, ext))
    train_normal_local["label_psl"] = labels.apply(lambda t: t[0])
    train_normal_local["label_status"] = labels.apply(lambda t: t[1])

    unique_labels = train_normal_local.loc[
        train_normal_local["label_status"] == "ok", "label_psl"
    ].unique()

    entropies = np.array([
        shannon_entropy(l) for l in unique_labels if l
    ], dtype=np.float64)
    return entropies


def _build_f_distribution(train_normal: pd.DataFrame) -> tuple[np.ndarray, int]:
    if not {"client_ip", "window_start", "event_id"}.issubset(train_normal.columns):
        train_normal_local = train_normal.copy()
        train_normal_local["qname_norm"] = train_normal_local["qname"].apply(normalize_qname)
        from features import align_to_window_start
        train_normal_local["window_start"] = train_normal_local["timestamp"].apply(align_to_window_start)
        grp = train_normal_local.groupby(["client_ip", "window_start"])
    else:
        grp = train_normal.groupby(["client_ip", "window_start"])

    f_values = grp.size().to_numpy()
    return f_values, int(len(f_values))


def _build_i_distribution(train_normal: pd.DataFrame) -> tuple[np.ndarray, int, int]:
    if not {"client_ip", "window_start", "timestamp"}.issubset(train_normal.columns):
        train_normal_local = train_normal.copy()
        train_normal_local["qname_norm"] = train_normal_local["qname"].apply(normalize_qname)
        from features import align_to_window_start
        train_normal_local["window_start"] = train_normal_local["timestamp"].apply(align_to_window_start)
    else:
        train_normal_local = train_normal.copy()

    def _interval(group: pd.DataFrame) -> Optional[float]:
        if len(group) < 3:
            return None
        ts = pd.to_datetime(group["timestamp"], utc=True).sort_values()
        diffs = ts.diff().dropna().dt.total_seconds()
        if len(diffs) == 0:
            return None
        return float(np.median(diffs.to_numpy()))

    intervals = (
        train_normal_local.groupby(["client_ip", "window_start"], group_keys=False)
        .apply(_interval)
    )
    total_windows = int(len(intervals))
    valid = intervals.dropna().to_numpy(dtype=np.float64)
    return valid, total_windows, int(len(valid))


def fit_baseline(log_df: pd.DataFrame,
                 H_quantile: float = 0.99,
                 F_quantile: float = 0.99,
                 I_quantile: float = 0.01,
                 window_seconds: int = 60) -> dict:
    train_normal = _filter_train_normal(log_df)

    h_dist = _build_h_distribution(train_normal)
    f_dist, n_windows = _build_f_distribution(train_normal)
    i_dist, n_windows_total, n_windows_valid = _build_i_distribution(train_normal)

    h_threshold = float(np.percentile(h_dist, H_quantile * 100, method="linear"))
    f_threshold = float(np.percentile(f_dist, F_quantile * 100, method="linear"))

    if len(i_dist) == 0:
        i_threshold = 0.0
        i_potentially_ineffective = True
    else:
        i_threshold = float(np.percentile(i_dist, I_quantile * 100, method="linear"))
        i_potentially_ineffective = (i_threshold == 0.0)

    baseline = {
        "version": BASELINE_SCHEMA_VERSION,
        "window_seconds": window_seconds,
        "fitted_on": {"split": "train", "label": 0},
        "fitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.mmmZ"),
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "pandas_version": pd.__version__,
        "psl_source": "tldextract==5.4.0 built-in snapshot (include_psl_private_domains=False, cache_dir=None, fallback_to_snapshot=False)",
        "features": {
            "H": {
                "quantile": H_quantile,
                "threshold": round(h_threshold, 4),
                "n_unique_labels": int(len(h_dist)),
                "n_missing": int(len([x for x in h_dist if np.isnan(x)])),
                "method": "numpy.percentile(q, method='linear')",
                "comparator": ">",
            },
            "F": {
                "quantile": F_quantile,
                "threshold": round(f_threshold, 4),
                "n_windows": n_windows,
                "method": "numpy.percentile(q, method='linear')",
                "comparator": ">",
            },
            "I": {
                "quantile": I_quantile,
                "threshold": round(i_threshold, 4),
                "n_windows_valid": n_windows_valid,
                "n_windows_total": n_windows_total,
                "i_rule_potentially_ineffective": i_potentially_ineffective,
                "method": "numpy.percentile(q, method='linear')",
                "comparator": "<",
            },
        },
        "freeze_check": "not frozen until D7",
    }

    return baseline


def fit_baseline_from_features(features_df: pd.DataFrame,
                               H_quantile: float = 0.99,
                               F_quantile: float = 0.99,
                               I_quantile: float = 0.01,
                               window_seconds: int = 60) -> dict:
    _validate_features_input(features_df)

    train_normal = features_df[
        (features_df["split"] == "train") & (features_df["label"] == 0)
    ].copy()
    if train_normal.empty:
        raise ValueError("No train+label=0 samples in features_df")

    h_dist = train_normal["H"].dropna().to_numpy(dtype=np.float64)
    f_dist = train_normal["F"].dropna().to_numpy(dtype=np.float64)

    i_total = len(train_normal)
    i_valid = train_normal["I"].notna().sum()
    i_dist = train_normal["I"].dropna().to_numpy(dtype=np.float64)

    if len(h_dist) == 0 or len(f_dist) == 0:
        raise ValueError("H or F distribution empty in train+label=0 samples")

    h_threshold = float(np.percentile(h_dist, H_quantile * 100, method="linear"))
    f_threshold = float(np.percentile(f_dist, F_quantile * 100, method="linear"))

    if len(i_dist) == 0:
        i_threshold = 0.0
        i_potentially_ineffective = True
    else:
        i_threshold = float(np.percentile(i_dist, I_quantile * 100, method="linear"))
        i_potentially_ineffective = (i_threshold == 0.0)

    baseline = {
        "version": BASELINE_SCHEMA_VERSION,
        "window_seconds": window_seconds,
        "fitted_on": {"split": "train", "label": 0},
        "fitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.mmmZ"),
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "pandas_version": pd.__version__,
        "psl_source": "tldextract==5.4.0 built-in snapshot",
        "features": {
            "H": {
                "quantile": H_quantile,
                "threshold": round(h_threshold, 4),
                "n_unique_labels": int(len(h_dist)),
                "n_missing": 0,
                "method": "numpy.percentile(q, method='linear')",
                "comparator": ">",
            },
            "F": {
                "quantile": F_quantile,
                "threshold": round(f_threshold, 4),
                "n_windows": int(len(f_dist)),
                "method": "numpy.percentile(q, method='linear')",
                "comparator": ">",
            },
            "I": {
                "quantile": I_quantile,
                "threshold": round(i_threshold, 4),
                "n_windows_valid": int(i_valid),
                "n_windows_total": int(i_total),
                "i_rule_potentially_ineffective": i_potentially_ineffective,
                "method": "numpy.percentile(q, method='linear')",
                "comparator": "<",
            },
        },
        "freeze_check": "not frozen until D7",
    }
    return baseline


def save_baseline(baseline: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, ensure_ascii=False)


def load_baseline(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"baseline.json not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("baseline.json root must be an object")
    missing = BASELINE_REQUIRED_KEYS - set(data.keys())
    if missing:
        raise ValueError(f"baseline.json missing required keys: {missing}")
    if data["fitted_on"] != {"split": "train", "label": 0}:
        raise ValueError(
            f"baseline fitted_on must be {{'split': 'train', 'label': 0}}, "
            f"got {data['fitted_on']}"
        )
    for feat in ["H", "F", "I"]:
        if feat not in data["features"]:
            raise ValueError(f"baseline.json features missing key: {feat}")
        inner = data["features"][feat]
        for sub in ["quantile", "threshold", "method", "comparator"]:
            if sub not in inner:
                raise ValueError(f"baseline.json features.{feat} missing key: {sub}")
    return data


def plot_baseline_histograms(features_df: pd.DataFrame,
                              baseline: dict,
                              output_dir: str) -> None:
    train_normal = features_df[
        (features_df["split"] == "train") & (features_df["label"] == 0)
    ]
    if train_normal.empty:
        return

    fig, axes = plt.subplots(1, 3, figsize=(15, 4))
    fig.suptitle("Baseline Histograms — Train Normal Only", fontsize=12)

    h_vals = train_normal["H"].dropna().to_numpy()
    f_vals = train_normal["F"].dropna().to_numpy()
    i_vals = train_normal["I"].dropna().to_numpy()

    axes[0].hist(h_vals, bins=30, color="steelblue", edgecolor="white")
    axes[0].axvline(baseline["features"]["H"]["threshold"],
                     color="red", linestyle="--", label=f"P{int(baseline['features']['H']['quantile']*100)}={baseline['features']['H']['threshold']:.2f}")
    axes[0].set_xlabel("Entropy H")
    axes[0].set_ylabel("Count")
    axes[0].set_title("H Distribution")
    axes[0].legend()

    axes[1].hist(f_vals, bins=30, color="seagreen", edgecolor="white")
    axes[1].axvline(baseline["features"]["F"]["threshold"],
                     color="red", linestyle="--", label=f"P{int(baseline['features']['F']['quantile']*100)}={baseline['features']['F']['threshold']:.1f}")
    axes[1].set_xlabel("Window Frequency F")
    axes[1].set_ylabel("Count")
    axes[1].set_title("F Distribution")
    axes[1].legend()

    axes[2].hist(i_vals, bins=30, color="orange", edgecolor="white")
    axes[2].axvline(baseline["features"]["I"]["threshold"],
                     color="red", linestyle="--", label=f"P{int(baseline['features']['I']['quantile']*100)}={baseline['features']['I']['threshold']:.2f}")
    axes[2].set_xlabel("Interval Median I (s)")
    axes[2].set_ylabel("Count")
    axes[2].set_title("I Distribution")
    axes[2].legend()

    plt.tight_layout()
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "baseline_histograms.png")
    plt.savefig(path, dpi=100)
    plt.close(fig)

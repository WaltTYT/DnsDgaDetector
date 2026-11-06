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

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
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


def _extract_unique_labels(train_normal: pd.DataFrame) -> np.ndarray:
    ext = _make_tldextract()
    local = train_normal.copy()
    local["qname_norm"] = local["qname"].apply(normalize_qname)
    labels = local["qname_norm"].apply(lambda q: extract_label(q, ext))
    local["label_psl"] = labels.apply(lambda t: t[0])
    local["label_status"] = labels.apply(lambda t: t[1])
    return local.loc[
        local["label_status"] == "ok", "label_psl"
    ].unique()


def _build_h_distribution(train_normal: pd.DataFrame) -> np.ndarray:
    unique_labels = _extract_unique_labels(train_normal)
    return np.array([
        shannon_entropy(l) for l in unique_labels if l
    ], dtype=np.float64)


def _label_stats(unique_labels: np.ndarray) -> dict:
    labels = [l for l in unique_labels if l]
    lengths = np.array([len(l) for l in labels], dtype=np.int64)
    digit_ratios = np.array(
        [sum(c.isdigit() for c in l) / len(l) for l in labels],
        dtype=np.float64,
    )
    return {
        "label_length_mean": round(float(lengths.mean()), 4),
        "digit_ratio_mean": round(float(digit_ratios.mean()), 4),
        "length_buckets": {
            "1-5": int((lengths <= 5).sum()),
            "6-10": int(((lengths > 5) & (lengths <= 10)).sum()),
            "11-15": int(((lengths > 10) & (lengths <= 15)).sum()),
            ">=16": int((lengths > 15).sum()),
        },
    }


def fit_baseline(log_df: pd.DataFrame,
                 H_quantile: float = 0.99,
                 F_quantile: float = 0.99,
                 I_quantile: float = 0.01,
                 window_seconds: int = 60) -> dict:
    from features import compute_features

    features_df = compute_features(log_df)
    return fit_baseline_from_features(
        features_df,
        H_quantile=H_quantile,
        F_quantile=F_quantile,
        I_quantile=I_quantile,
        window_seconds=window_seconds,
    )


def fit_baseline_from_features(features_df: pd.DataFrame,
                               H_quantile: float = 0.99,
                               F_quantile: float = 0.99,
                               I_quantile: float = 0.01,
                               window_seconds: int = 60) -> dict:
    _validate_features_input(features_df)

    train = features_df[features_df["split"] == "train"].copy()
    train_normal = train[train["label"] == 0].copy()
    if train_normal.empty:
        raise ValueError("No train+label=0 samples in features_df")

    unique_labels = _extract_unique_labels(train_normal)
    h_dist = _build_h_distribution(train_normal)

    windows = (
        train.groupby(["client_ip", "window_start"], as_index=False)
        .agg(max_label=("label", "max"), F=("F", "first"), I=("I", "first"))
    )
    pure_windows = windows[windows["max_label"] == 0]
    f_dist = pure_windows["F"].dropna().to_numpy(dtype=np.float64)
    i_dist = pure_windows["I"].dropna().to_numpy(dtype=np.float64)

    i_total = int(len(pure_windows))
    i_valid = int(len(i_dist))

    if len(h_dist) == 0 or len(f_dist) == 0:
        raise ValueError("H or F distribution empty in train+label=0 samples")

    h_stats = _label_stats(unique_labels)
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
        "fitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "pandas_version": pd.__version__,
        "psl_source": "data/raw/public_suffix_list.dat (include_psl_private_domains=True, cache_dir=None, fallback_to_snapshot=False)",
        "features": {
            "H": {
                "quantile": H_quantile,
                "threshold": round(h_threshold, 4),
                "n_unique_labels": int(len(h_dist)),
                "n_missing": int(np.isnan(h_dist).sum()),
                "method": "numpy.percentile(q, method='linear')",
                "comparator": ">",
                **h_stats,
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
                "n_windows_valid": i_valid,
                "n_windows_total": i_total,
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
    fig.suptitle("基线分布 —— 仅训练集正常样本", fontsize=12)

    h_vals = train_normal["H"].dropna().to_numpy()
    f_vals = train_normal["F"].dropna().to_numpy()
    i_vals = train_normal["I"].dropna().to_numpy()

    axes[0].hist(h_vals, bins=30, color="steelblue", edgecolor="white")
    axes[0].axvline(baseline["features"]["H"]["threshold"],
                     color="red", linestyle="--", label=f"P{int(baseline['features']['H']['quantile']*100)} 阈值={baseline['features']['H']['threshold']:.2f}")
    axes[0].set_xlabel("熵 H")
    axes[0].set_ylabel("样本数")
    axes[0].set_title("H 分布")
    axes[0].legend()

    axes[1].hist(f_vals, bins=30, color="seagreen", edgecolor="white")
    axes[1].axvline(baseline["features"]["F"]["threshold"],
                     color="red", linestyle="--", label=f"P{int(baseline['features']['F']['quantile']*100)} 阈值={baseline['features']['F']['threshold']:.1f}")
    axes[1].set_xlabel("窗口频次 F")
    axes[1].set_ylabel("样本数")
    axes[1].set_title("F 分布")
    axes[1].legend()

    axes[2].hist(i_vals, bins=30, color="orange", edgecolor="white")
    axes[2].axvline(baseline["features"]["I"]["threshold"],
                     color="red", linestyle="--", label=f"P{int(baseline['features']['I']['quantile']*100)} 阈值={baseline['features']['I']['threshold']:.2f}")
    axes[2].set_xlabel("间隔中位数 I（秒）")
    axes[2].set_ylabel("样本数")
    axes[2].set_title("I 分布")
    axes[2].legend()

    plt.tight_layout()
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "baseline_histograms.png")
    plt.savefig(path, dpi=100)
    plt.close(fig)

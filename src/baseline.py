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
# 无界面后端，保证在无显示环境（批处理/远程）也能出图
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# 指定中文字体回退链，并让坐标轴负号正常显示
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


# baseline.json 结构版本号，规则引擎加载时会强制比对，版本不符直接拒绝
BASELINE_SCHEMA_VERSION = 1

# 加载基线时必须齐备的顶层键，缺失说明文件被截断或版本过旧
BASELINE_REQUIRED_KEYS = {
    "version", "window_seconds", "fitted_on", "fitted_at",
    "python_version", "numpy_version", "pandas_version", "psl_source",
    "features", "freeze_check",
}


def _validate_features_input(df: pd.DataFrame) -> None:
    # 确认输入是契约列顺序的特征表，并检查 split 取值合法
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
    # 从训练集正常样本提取去重后的可注册域标签，只保留解析成功的 ok 状态
    ext = _make_tldextract()
    # 在副本上派生中间列，不改动调用方数据
    local = train_normal.copy()
    local["qname_norm"] = local["qname"].apply(normalize_qname)
    labels = local["qname_norm"].apply(lambda q: extract_label(q, ext))
    local["label_psl"] = labels.apply(lambda t: t[0])
    local["label_status"] = labels.apply(lambda t: t[1])
    # 空串、IP、punycode、无域标签都会被过滤，只留真正可算熵的标签
    return local.loc[
        local["label_status"] == "ok", "label_psl"
    ].unique()


def _build_h_distribution(train_normal: pd.DataFrame) -> np.ndarray:
    # 把唯一标签逐个算熵，组成后续取 P99 的分布数组
    unique_labels = _extract_unique_labels(train_normal)
    # 再次剔除空标签避免 0 值污染分布；显式 float64 便于分位数计算
    return np.array([
        shannon_entropy(l) for l in unique_labels if l
    ], dtype=np.float64)


def _label_stats(unique_labels: np.ndarray) -> dict:
    # 统计正常标签的长度分布与数字占比，仅用于报告描述，不参与阈值拟合
    labels = [l for l in unique_labels if l]
    lengths = np.array([len(l) for l in labels], dtype=np.int64)
    # 逐标签计算数字字符占比；标签非空由上一行过滤保证，不会除零
    digit_ratios = np.array(
        [sum(c.isdigit() for c in l) / len(l) for l in labels],
        dtype=np.float64,
    )
    # 长度分桶便于对比 DGA 与正常域名的形态差异
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
    # 便捷入口：先把原始日志算成特征表，再走特征版拟合
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
    # 从特征表拟合三项阈值，输出可直接序列化为 baseline.json 的字典
    _validate_features_input(features_df)

    # 数据隔离红线：只取 split=train，验证/测试集一律不参与统计
    train = features_df[features_df["split"] == "train"].copy()
    # 再过滤 label=0，DGA 样本不得进入正常分布
    train_normal = train[train["label"] == 0].copy()
    if train_normal.empty:
        raise ValueError("No train+label=0 samples in features_df")

    unique_labels = _extract_unique_labels(train_normal)
    h_dist = _build_h_distribution(train_normal)

    # 窗口级 F/I 按客户端+窗口去重；max_label 用于判断窗口是否纯正常
    windows = (
        train.groupby(["client_ip", "window_start"], as_index=False)
        .agg(max_label=("label", "max"), F=("F", "first"), I=("I", "first"))
    )
    # 只保留不含 DGA 的窗口，避免异常行为抬高 F 阈值、压低 I 阈值
    pure_windows = windows[windows["max_label"] == 0]
    f_dist = pure_windows["F"].dropna().to_numpy(dtype=np.float64)
    i_dist = pure_windows["I"].dropna().to_numpy(dtype=np.float64)

    # I 只有查询数 >=3 的窗口才有值，需同时记录总窗口数与有效窗口数
    i_total = int(len(pure_windows))
    i_valid = int(len(i_dist))

    if len(h_dist) == 0 or len(f_dist) == 0:
        # 分布为空时无法确定阈值，直接失败而不是产出无意义基线
        raise ValueError("H or F distribution empty in train+label=0 samples")

    h_stats = _label_stats(unique_labels)
    # 统一用线性插值分位数，并在输出中记录 method 便于复现
    h_threshold = float(np.percentile(h_dist, H_quantile * 100, method="linear"))
    f_threshold = float(np.percentile(f_dist, F_quantile * 100, method="linear"))

    # 没有有效间隔样本时阈值退化为 0，并标记间隔规则可能无效
    if len(i_dist) == 0:
        i_threshold = 0.0
        i_potentially_ineffective = True
    else:
        i_threshold = float(np.percentile(i_dist, I_quantile * 100, method="linear"))
        # P1 恰为 0 意味着 I < 0 永不成立，间隔规则形同虚设
        i_potentially_ineffective = (i_threshold == 0.0)

    baseline = {
        "version": BASELINE_SCHEMA_VERSION,
        "window_seconds": window_seconds,
        # 记录拟合来源，load_baseline 会强制校验必须为 train + label=0
        "fitted_on": {"split": "train", "label": 0},
        # UTC 时间戳，微秒截断到毫秒后补 Z
        "fitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        # 记录运行环境版本，便于复现与排查分位数差异
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "pandas_version": pd.__version__,
        # 固定记录 PSL 离线配置，确保阈值可用同一后缀表复算
        "psl_source": "data/raw/public_suffix_list.dat (include_psl_private_domains=True, cache_dir=None, fallback_to_snapshot=False)",
        "features": {
            # H 严格大于阈值才判为异常，附带标签描述统计
            "H": {
                "quantile": H_quantile,
                "threshold": round(h_threshold, 4),
                "n_unique_labels": int(len(h_dist)),
                "n_missing": int(np.isnan(h_dist).sum()),
                "method": "numpy.percentile(q, method='linear')",
                "comparator": ">",
                **h_stats,
            },
            # F 严格大于阈值；n_windows 记录参与拟合的纯正常窗口数
            "F": {
                "quantile": F_quantile,
                "threshold": round(f_threshold, 4),
                "n_windows": int(len(f_dist)),
                "method": "numpy.percentile(q, method='linear')",
                "comparator": ">",
            },
            # I 严格小于阈值；单独记录有效/总窗口数，便于判断样本是否充足
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
        # D7 前基线尚未冻结，允许重新拟合
        "freeze_check": "not frozen until D7",
    }
    return baseline


def save_baseline(baseline: dict, path: str) -> None:
    # 写入 UTF-8 JSON，自动创建父目录，保留中文不转义
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, ensure_ascii=False)


def load_baseline(path: str) -> dict:
    # 读取并逐层校验基线文件，宁可报错也不返回结构不完整的基线
    if not os.path.exists(path):
        raise FileNotFoundError(f"baseline.json not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("baseline.json root must be an object")
    # 顶层键必须齐全，缺键通常意味着文件被手改或版本不匹配
    missing = BASELINE_REQUIRED_KEYS - set(data.keys())
    if missing:
        raise ValueError(f"baseline.json missing required keys: {missing}")
    # 防止误用非训练集拟合的基线，这是数据隔离的最后一道校验
    if data["fitted_on"] != {"split": "train", "label": 0}:
        raise ValueError(
            f"baseline fitted_on must be {{'split': 'train', 'label': 0}}, "
            f"got {data['fitted_on']}"
        )
    # 三项特征及其必需子键都要存在，规则引擎才能直接取用
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
    # 画 H/F/I 三张训练集正常样本直方图，并用红色虚线标出阈值
    train_normal = features_df[
        (features_df["split"] == "train") & (features_df["label"] == 0)
    ]
    if train_normal.empty:
        # 没有正常训练样本时不产出空图
        return

    # 一行三列布局，分别对应三项特征
    fig, axes = plt.subplots(1, 3, figsize=(15, 4))
    fig.suptitle("基线分布 —— 仅训练集正常样本", fontsize=12)

    # 绘图前剔除 NaN，直方图不接受缺失值
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
    # 显式关闭 figure，避免批量调用时内存持续累积
    plt.close(fig)

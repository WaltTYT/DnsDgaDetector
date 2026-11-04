"""规则检测引擎。

负责人：组员B（D6）
参考：docs/specs/project-roadmap.md 2.3 规则定义、
      docs/plans/d4-d6-design.md §3

规则定义（全部严格比较）：
  E = H > baseline.H.threshold           熵规则命中
  B = (F > baseline.F.threshold)        行为规则 = 频率规则 OR 间隔规则
      OR (I < baseline.I.threshold)     间隔规则：缺失时跳过（False）
  R = E OR B                            最终规则告警

核心约束：
- 阈值候选只在 split=val 上比较，绝不触碰 test
- E/B/R 只由 H/F/I 和阈值决定，不读取 label/source/algorithm/split 做决策
- I 缺失时间隔规则不触发，B 只由频率规则决定
- 客户端行为规则（B）可能连带标记同窗口正常域名，需显式统计
"""

from __future__ import annotations

import itertools
from typing import Any, Optional

import numpy as np
import pandas as pd

from baseline import BASELINE_SCHEMA_VERSION, load_baseline
from features import FEATURES_COLUMNS


def compute_rules(features_df: pd.DataFrame, baseline: dict) -> pd.DataFrame:
    _validate_baseline(baseline)
    _validate_features_input(features_df)

    out = features_df.copy()

    h_thr = baseline["features"]["H"]["threshold"]
    f_thr = baseline["features"]["F"]["threshold"]
    i_thr = baseline["features"]["I"]["threshold"]

    out["E"] = (out["H"] > h_thr).astype(int)

    freq_hit = out["F"].notna() & (out["F"] > f_thr)
    interval_hit = out["I"].notna() & (out["I"] < i_thr)
    out["B"] = (freq_hit | interval_hit).astype(int)

    out["R"] = ((out["E"] == 1) | (out["B"] == 1)).astype(int)

    return out


def compute_rules_thresholded(features_df: pd.DataFrame,
                               H_threshold: float,
                               F_threshold: float,
                               I_threshold: float) -> pd.DataFrame:
    out = features_df.copy()
    out["E"] = (out["H"] > H_threshold).astype(int)
    freq_hit = out["F"].notna() & (out["F"] > F_threshold)
    interval_hit = out["I"].notna() & (out["I"] < I_threshold)
    out["B"] = (freq_hit | interval_hit).astype(int)
    out["R"] = ((out["E"] == 1) | (out["B"] == 1)).astype(int)
    return out


def _validate_baseline(baseline: dict) -> None:
    if not isinstance(baseline, dict):
        raise ValueError("baseline must be a dict")
    if baseline.get("version") != BASELINE_SCHEMA_VERSION:
        raise ValueError(
            f"baseline version mismatch: expected {BASELINE_SCHEMA_VERSION}, "
            f"got {baseline.get('version')}"
        )
    for feat in ["H", "F", "I"]:
        inner = baseline.get("features", {}).get(feat)
        if not inner:
            raise ValueError(f"baseline.features.{feat} missing")
        if "threshold" not in inner:
            raise ValueError(f"baseline.features.{feat}.threshold missing")


def _validate_features_input(df: pd.DataFrame) -> None:
    actual = list(df.columns)
    if actual != FEATURES_COLUMNS:
        raise ValueError(
            f"features columns mismatch.\n"
            f"  expected: {FEATURES_COLUMNS}\n"
            f"  actual:   {actual}"
        )
    for feat_col in ["H", "F", "I"]:
        if feat_col not in df.columns:
            raise ValueError(f"features missing column: {feat_col}")


def _confusion_matrix(df: pd.DataFrame, truth_col: str, pred_col: str) -> dict:
    tp = int(((df[truth_col] == 1) & (df[pred_col] == 1)).sum())
    fp = int(((df[truth_col] == 0) & (df[pred_col] == 1)).sum())
    fn = int(((df[truth_col] == 1) & (df[pred_col] == 0)).sum())
    tn = int(((df[truth_col] == 0) & (df[pred_col] == 0)).sum())
    total = tp + fp + fn + tn

    fpr = fp / (fp + tn) if (fp + tn) > 0 else float("nan")
    fnr = fn / (fn + tp) if (fn + tp) > 0 else float("nan")
    precision = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
    recall = tp / (tp + fn) if (tp + fn) > 0 else float("nan")

    return {
        "TP": tp, "FP": fp, "FN": fn, "TN": tn, "total": total,
        "FPR": fpr, "FNR": fnr,
        "Precision": precision, "Recall": recall,
    }


def evaluate_candidates(features_df: pd.DataFrame,
                        H_values: list[float],
                        F_values: list[float],
                        I_values: list[float]) -> list[dict]:
    _validate_features_input(features_df)

    val_df = features_df[features_df["split"] == "val"].copy()
    if val_df.empty:
        raise ValueError("No split=val samples found for candidate evaluation")

    results = []
    for h_q, f_q, i_q in itertools.product(H_values, F_values, I_values):
        temp_baseline = {
            "version": BASELINE_SCHEMA_VERSION,
            "features": {
                "H": {"threshold": h_q},
                "F": {"threshold": f_q},
                "I": {"threshold": i_q},
            }
        }
        scored = compute_rules(val_df, temp_baseline)
        metrics = _confusion_matrix(scored, truth_col="label", pred_col="R")
        results.append({
            "H_threshold": h_q,
            "F_threshold": f_q,
            "I_threshold": i_q,
            **metrics,
        })
    return results


def summarize_client_contamination(features_df: pd.DataFrame,
                                    baseline: dict) -> dict:
    scored = compute_rules(features_df, baseline)

    summary_by_split = {}
    for split in ["train", "val", "test"]:
        sdf = scored[scored["split"] == split]
        if sdf.empty:
            continue
        normal_total = int((sdf["label"] == 0).sum())
        dga_total = int((sdf["label"] == 1).sum())

        normal_b_hit = int(((sdf["label"] == 0) & (sdf["B"] == 1)).sum())
        normal_r_hit = int(((sdf["label"] == 0) & (sdf["R"] == 1)).sum())
        normal_b_only = int(((sdf["label"] == 0) & (sdf["B"] == 1) & (sdf["E"] == 0)).sum())

        summary_by_split[split] = {
            "normal_samples": normal_total,
            "dga_samples": dga_total,
            "normal_with_B_hit": normal_b_hit,
            "normal_with_R_hit": normal_r_hit,
            "normal_with_B_only_hit": normal_b_only,
            "normal_B_hit_rate": (normal_b_hit / normal_total) if normal_total > 0 else 0.0,
        }

    overall_normal = int((scored["label"] == 0).sum())
    overall_normal_b = int(((scored["label"] == 0) & (scored["B"] == 1)).sum())
    overall = {
        "normal_samples": overall_normal,
        "dga_samples": int((scored["label"] == 1).sum()),
        "normal_with_B_hit": overall_normal_b,
        "normal_B_hit_rate": (overall_normal_b / overall_normal) if overall_normal > 0 else 0.0,
    }

    return {
        "overall": overall,
        "by_split": summary_by_split,
        "note": "B=1 命中可能来自客户端级行为规则（F 或 I），混合窗口中正常域名继承同一窗口的 F/I，可能连带被标记。",
    }


def get_rule_evidence(row: pd.Series, baseline: dict) -> str:
    ev = []
    h_thr = baseline["features"]["H"]["threshold"]
    f_thr = baseline["features"]["F"]["threshold"]
    i_thr = baseline["features"]["I"]["threshold"]

    h_val = row.get("H")
    f_val = row.get("F")
    i_val = row.get("I")

    e_hit = (h_val is not None and pd.notna(h_val) and h_val > h_thr)
    freq_hit = (f_val is not None and pd.notna(f_val) and f_val > f_thr)
    interval_hit = (i_val is not None and pd.notna(i_val) and i_val < i_thr)
    interval_missing = (i_val is None or pd.isna(i_val))

    ev.append(f"E={'1' if e_hit else '0'} (H={h_val if pd.notna(h_val) else 'NaN'} > {h_thr})")
    ev.append(f"freq={'1' if freq_hit else '0'} (F={f_val} > {f_thr})")
    if interval_missing:
        ev.append(f"interval=skipped (I missing)")
    else:
        ev.append(f"interval={'1' if interval_hit else '0'} (I={i_val} < {i_thr})")

    b_hit = freq_hit or interval_hit
    r_hit = e_hit or b_hit
    ev.append(f"B={'1' if b_hit else '0'}, R={'1' if r_hit else '0'}")

    return "; ".join(ev)

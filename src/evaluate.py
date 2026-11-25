"""评估：两方案混淆矩阵与指标对比。

负责人：组长（D10–D12）
参考：docs/specs/project-roadmap.md 2.2 双口径、阶段4 指标定义

待实现要点：
- 主口径：聚合样本 (client_ip, window_start, qname) 上计算 TP/FP/FN/TN
- 副口径：唯一完整域名级（任一窗口告警即检出），两口径分表
- FPR=FP/(FP+TN)、FNR=FN/(FN+TP)、Precision、Recall，可补 F1；
  分母为零输出 N/A 并显示支持样本数；不平衡场景不以 accuracy 为主结论
- 自动校验：混合阳性集合必须是纯规则阳性集合的子集
- 同时报告正确撤回 FP 数与错误放行 TP 数、可复核覆盖率、不确定率、失败率、调用成本
- 手算基准：TP=8,FP=2,FN=2,TN=18 → FPR=10%、FNR=20%、P=80%、R=80%
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# 指定中文可用字体并修正负号显示，保证图表中文标签不乱码
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
import numpy as np
import pandas as pd

# 以脚本方式运行时走扁平导入，作为包被导入时退回相对导入
try:
    from features import FEATURES_COLUMNS
    from llm_judge import (
        VERDICTS,
        cache_key,
        generation_params,
        load_cache,
        sanitize_endpoint,
        template_hash,
    )
except ImportError:
    from .features import FEATURES_COLUMNS
    from .llm_judge import (
        VERDICTS,
        cache_key,
        generation_params,
        load_cache,
        sanitize_endpoint,
        template_hash,
    )


# 主口径聚合键：样本粒度由客户端 IP、窗口起点与查询域名共同确定
AGGREGATE_KEYS = ["client_ip", "window_start", "qname"]


def _validate_features(df: pd.DataFrame) -> None:
    # 要求列名与顺序严格等于特征契约，不满足即报错，避免口径错位
    actual = list(df.columns)
    if actual != FEATURES_COLUMNS:
        raise ValueError(
            f"features columns mismatch.\n"
            f"  expected: {FEATURES_COLUMNS}\n"
            f"  actual:   {actual}"
        )


def _validate_feature_values(df: pd.DataFrame) -> None:
    # 校验各列取值域：标记与规则列为 0/1，split 仅限三种划分，llm_verdict 仅限枚举或空
    for column in ("label", "E", "B", "R"):
        if not df[column].isin([0, 1]).all():
            raise ValueError(f"invalid {column} values: expected 0/1")
    if not df["split"].isin(["train", "val", "test"]).all():
        raise ValueError("invalid split values: expected train/val/test")
    verdict = df["llm_verdict"].astype("string").fillna("")
    if not verdict.isin(["", "na", "DGA", "normal", "uncertain"]).all():
        raise ValueError("invalid llm_verdict values")
    # hybrid 允许尚为空（未复核），但已有值只能是 0/1
    hybrid_values = set(df["hybrid"].dropna().unique())
    if not hybrid_values <= {0, 1}:
        raise ValueError("invalid hybrid values: expected 0/1 or empty")


def _entry_field(entry: Any, name: str, default: Any = None) -> Any:
    # 兼容复核条目为 dict 或对象两种形态，统一按字段名取值并支持缺省值
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def apply_hybrid(features_df: pd.DataFrame,
                 verdicts: dict[str, Any]) -> pd.DataFrame:
    # 输入特征表与 qname 到复核结果的映射，输出写回 llm_verdict/llm_fail_reason/hybrid 三列
    _validate_features(features_df)
    _validate_feature_values(features_df)
    out = features_df.copy()
    llm_verdicts: list[str] = []
    fail_reasons: list[str] = []
    hybrids: list[int] = []
    for row in out.itertuples(index=False):
        entry = verdicts.get(row.qname)
        # 无复核记录时记为 na，后续按“未排除”处理，仍保留告警
        if entry is None:
            verdict = "na"
            fail_reason = ""
        else:
            verdict = str(_entry_field(entry, "verdict", "uncertain"))
            if verdict not in VERDICTS:
                raise ValueError(f"invalid verdict for qname {row.qname}: {verdict}")
            fail_reason = _entry_field(entry, "fail_reason", None) or ""
            fail_reason = str(fail_reason)
            # 存在失败原因的条目必须落在 uncertain，防止失败被当作确定结论
            if fail_reason and verdict != "uncertain":
                raise ValueError(
                    f"failed verdict must be uncertain for qname {row.qname}"
                )
        e_hit = pd.notna(row.E) and int(row.E) == 1
        b_hit = pd.notna(row.B) and int(row.B) == 1
        # 混合判据：B 命中即告警；仅 E 命中时看 LLM 是否明确判为 normal，其余（含 na/uncertain）保留告警
        hybrid = 1 if b_hit or (e_hit and verdict != "normal") else 0
        llm_verdicts.append(verdict)
        fail_reasons.append(fail_reason)
        hybrids.append(hybrid)

    out["llm_verdict"] = llm_verdicts
    out["llm_fail_reason"] = fail_reasons
    out["hybrid"] = hybrids
    return out[FEATURES_COLUMNS]


def compute_metrics(df: pd.DataFrame, pred_col: str, split: str) -> dict:
    # 单口径混淆矩阵：按 split 过滤后统计 TP/FP/FN/TN，并给出四项比率与正负支持样本数
    if pred_col not in df.columns:
        raise ValueError(f"prediction column missing: {pred_col}")
    sdf = df[df["split"] == split]
    if not sdf["label"].isin([0, 1]).all():
        raise ValueError("Invalid label values in evaluation split")
    if not sdf[pred_col].isin([0, 1]).all():
        raise ValueError(f"Invalid prediction values in {pred_col}")
    tp = int(((sdf["label"] == 1) & (sdf[pred_col] == 1)).sum())
    fp = int(((sdf["label"] == 0) & (sdf[pred_col] == 1)).sum())
    fn = int(((sdf["label"] == 1) & (sdf[pred_col] == 0)).sum())
    tn = int(((sdf["label"] == 0) & (sdf[pred_col] == 0)).sum())

    # 分母为零时返回 None（下游显示 N/A），否则四舍五入保留 4 位
    def ratio(numerator: int, denominator: int) -> float | None:
        if denominator == 0:
            return None
        return round(numerator / denominator, 4)

    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "total": tp + fp + fn + tn,
        "fpr": ratio(fp, fp + tn),
        "fnr": ratio(fn, fn + tp),
        "precision": ratio(tp, tp + fp),
        "recall": ratio(tp, tp + fn),
        "positive_support": tp + fn,
        "negative_support": fp + tn,
    }


def _rule_hit_series(df: pd.DataFrame) -> pd.Series:
    # 把 E/B 两路命中编码为 none/E_only/B_only/E_and_B，用于按命中来源分组
    e_hit = df["E"].fillna(0).astype(int) == 1
    b_hit = df["B"].fillna(0).astype(int) == 1
    series = pd.Series("none", index=df.index, dtype="object")
    series.loc[e_hit & ~b_hit] = "E_only"
    series.loc[~e_hit & b_hit] = "B_only"
    series.loc[e_hit & b_hit] = "E_and_B"
    return series


def _group_metrics(df: pd.DataFrame, group_col: str) -> dict:
    # 按指定列分组，逐组输出样本量、标签分布以及规则/混合两套指标
    if df.empty:
        return {}
    normalized = df.copy()
    # 缺失或空字符串统一归为 none 分组，避免分组键为空
    normalized[group_col] = (
        normalized[group_col].fillna("none").replace("", "none").astype(str)
    )
    result = {}
    for name, group in normalized.groupby(group_col, sort=True):
        split_value = str(group["split"].iloc[0])
        result[str(name)] = {
            "rows": int(len(group)),
            "label_counts": {
                "0": int((group["label"] == 0).sum()),
                "1": int((group["label"] == 1).sum()),
            },
            "rule": compute_metrics(group, "R", split_value),
            "hybrid": compute_metrics(group, "hybrid", split_value),
        }
    return result


def grouped_metrics(df: pd.DataFrame, split: str) -> dict:
    # 分组指标入口：按数据来源、生成算法、规则命中类型三个维度统计
    sdf = df[df["split"] == split]
    # 规则命中类型只在规则阳性子集上有意义
    rule_df = sdf[sdf["R"] == 1].copy()
    rule_df["rule_hit"] = _rule_hit_series(rule_df)
    return {
        "by_source": _group_metrics(sdf, "source"),
        "by_algorithm": _group_metrics(sdf, "algorithm"),
        "by_rule_hit": _group_metrics(rule_df, "rule_hit"),
    }


def _json_value(value: Any) -> Any:
    # 把 NaN/NA 等缺失值转成 None，其余基本类型原样保留，便于 JSON 序列化
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def _sample_excerpts(df: pd.DataFrame) -> list[dict]:
    # 最多摘取 5 条样本明细；仅 source=manual_dga 的记录附加 qname 字段
    samples = []
    for row in df.head(5).itertuples(index=False):
        item = {
            "client_ip": _json_value(row.client_ip),
            "window_start": _json_value(row.window_start),
            "label": _json_value(row.label),
            "source": _json_value(row.source),
            "E": _json_value(row.E),
            "B": _json_value(row.B),
            "R": _json_value(row.R),
            "llm_verdict": _json_value(row.llm_verdict),
            "llm_fail_reason": _json_value(row.llm_fail_reason),
            "hybrid": _json_value(row.hybrid),
        }
        if str(_json_value(row.source)) == "manual_dga":
            item["qname"] = _json_value(row.qname)
        samples.append(item)
    return samples


def reversal_detail(df: pd.DataFrame, split: str) -> dict:
    # 改判明细：在可复核子集（E=1 且 B=0）上区分正确撤回 FP、错误放行 TP、保留告警与未改判
    sdf = df[df["split"] == split]
    base = (sdf["E"] == 1) & (sdf["B"] == 0)
    verdict = sdf["llm_verdict"].fillna("").astype(str)
    fail_reason = sdf["llm_fail_reason"].fillna("").astype(str)
    label = sdf["label"]

    categories = {
        # 真负样本被判 normal：正确撤回
        "correct_withdraw_fp": base & (label == 0) & (verdict == "normal"),
        # 真正样本被判 normal：错误放行
        "wrong_release_tp": base & (label == 1) & (verdict == "normal"),
        # 不确定或调用失败：保留告警并单列原因
        "retained_uncertain_or_fail": base & ((verdict == "uncertain") | (fail_reason != "")),
        # LLM 判为 DGA：维持告警
        "no_reversal": base & (verdict == "DGA"),
    }
    return {
        name: {"count": int(mask.sum()), "samples": _sample_excerpts(sdf[mask])}
        for name, mask in categories.items()
    }


def _key_set(df: pd.DataFrame, pred_col: str) -> set[tuple]:
    # 取出预测为阳性的样本聚合键集合，供子集性质校验
    missing = [name for name in AGGREGATE_KEYS if name not in df.columns]
    if missing:
        raise ValueError(f"aggregate key columns missing: {missing}")
    mask = df[pred_col] == 1
    return set(
        df.loc[mask, AGGREGATE_KEYS].itertuples(index=False, name=None)
    )


def assert_subset_property(rule: Any, hybrid: Any, split: str) -> bool:
    # 校验混合方案阳性（含 TP）必为规则阳性的子集；支持 DataFrame 与已算好的指标 dict 两种入参
    if isinstance(rule, pd.DataFrame) and isinstance(hybrid, pd.DataFrame):
        rule_df = rule[rule["split"] == split]
        hybrid_df = hybrid[hybrid["split"] == split]
        rule_positive = _key_set(rule_df, "R")
        hybrid_positive = _key_set(hybrid_df, "hybrid")
        # 混合阳性中出现规则未命中样本即视为违反子集性质
        extra = sorted(hybrid_positive - rule_positive)
        if extra:
            raise AssertionError(
                f"hybrid positives not subset of rule positives on split={split}: "
                f"{extra[:5]}"
            )
        rule_tp = _key_set(rule_df[rule_df["label"] == 1], "R")
        hybrid_tp = _key_set(hybrid_df[hybrid_df["label"] == 1], "hybrid")
        extra_tp = sorted(hybrid_tp - rule_tp)
        if extra_tp:
            raise AssertionError(
                f"hybrid TP not subset of rule TP on split={split}: {extra_tp[:5]}"
            )
        return True

    if isinstance(rule, dict) and isinstance(hybrid, dict):
        # 指标口径下混合不应多于规则：FP/TP 不增、TN 不减
        if (hybrid["fp"] > rule["fp"] or hybrid["tp"] > rule["tp"]
                or hybrid["tn"] < rule["tn"]):
            raise AssertionError(
                f"subset property violated on split={split}: "
                f"rule={rule} hybrid={hybrid}"
            )
        return True

    raise TypeError("rule and hybrid must both be DataFrames or both be dicts")


def unique_domain_metrics(df: pd.DataFrame, split: str) -> dict:
    # 副口径：按唯一完整域名折叠，label 取首值，R/hybrid 取最大值（任一窗口命中即算检出）
    sdf = df[df["split"] == split]
    # 空划分无法折叠，直接返回空样本的指标结果
    if sdf.empty:
        return {
            "rule": compute_metrics(sdf, "R", split),
            "hybrid": compute_metrics(sdf, "hybrid", split),
            "unique_domains": 0,
        }
    label_counts = sdf.groupby("qname")["label"].nunique()
    # 同一域名在多窗口下标签必须一致，否则折叠不可靠
    if (label_counts > 1).any():
        raise ValueError("conflicting labels for the same qname")
    collapsed = (
        sdf.groupby("qname", as_index=False)
        .agg(label=("label", "first"), R=("R", "max"), hybrid=("hybrid", "max"))
    )
    collapsed["split"] = split
    return {
        "rule": compute_metrics(collapsed, "R", split),
        "hybrid": compute_metrics(collapsed, "hybrid", split),
        "unique_domains": int(len(collapsed)),
    }


def _sha256_file(path: Path) -> str:
    # 分块读取并计算文件 SHA-256，用于登记输入指纹
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest(path: Path) -> dict:
    # 读取运行清单；文件缺失或为空时返回空 dict，后续按无历史信息处理
    if not path.exists() or path.stat().st_size == 0:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("run_manifest.json root must be an object")
    return data


def _verdicts_from_cache(cache: dict[str, dict], model_cfg: dict,
                         template_hash_value: str) -> dict[str, dict]:
    # 从缓存筛出与当前模板哈希/模型/参数/端点一致且缓存键可复算命中的条目，转为 qname 到判定
    params = generation_params(model_cfg)
    endpoint = sanitize_endpoint(model_cfg["base_url"])
    verdicts: dict[str, dict] = {}
    # 四项配置任一不一致即视为过期缓存，直接跳过
    for entry in cache.values():
        if entry["prompt_template_hash"] != template_hash_value:
            continue
        if entry["model"] != model_cfg["name"]:
            continue
        if entry["params"] != params:
            continue
        if entry["endpoint"] != endpoint:
            continue
        input_obj = entry["input"]
        # 重新计算缓存键，确保条目字段未被篡改或与当前输入不一致
        expected_key = cache_key(
            input_obj["qname"], input_obj["registrable_label"], model_cfg["name"],
            template_hash_value, params, endpoint,
        )
        if expected_key != entry["cache_key"]:
            continue
        # 同一 qname 只允许一条有效判定，出现重复说明缓存被污染
        if input_obj["qname"] in verdicts:
            raise ValueError(f"duplicate cache verdict for qname {input_obj['qname']}")
        parsed = entry["parsed"]
        verdicts[input_obj["qname"]] = {
            "verdict": parsed["verdict"],
            "reason": parsed.get("reason", ""),
            "fail_reason": None,
        }
    return verdicts


def _cache_coverage(df: pd.DataFrame, split: str,
                    verdicts: dict[str, dict]) -> dict:
    # 可复核覆盖率：可复核子集（E=1 且 B=0）内唯一 qname 中被有效缓存覆盖的比例
    sdf = df[(df["split"] == split) & (df["E"] == 1) & (df["B"] == 0)]
    qnames = set(sdf["qname"])
    covered = len(qnames & set(verdicts))
    return {
        "judgeable_unique": len(qnames),
        "covered": covered,
        "rate": round(covered / len(qnames), 4) if qnames else None,
    }


# 对比图展示的四项指标及固定顺序
METRIC_LABELS = ["fpr", "fnr", "precision", "recall"]


def plot_metric_comparison(metrics: dict, output_dir: str) -> str:
    # 用测试集指标绘制规则与混合方案对比柱状图，返回图片路径
    rule_values = [metrics["rule"]["test"][key] for key in METRIC_LABELS]
    hybrid_values = [metrics["hybrid"]["test"][key] for key in METRIC_LABELS]
    # 指标为 None（分母为零）时用 NaN 占位，图中另行标注“无数据”
    rule_heights = [float("nan") if value is None else float(value) for value in rule_values]
    hybrid_heights = [float("nan") if value is None else float(value) for value in hybrid_values]

    fig, ax = plt.subplots(figsize=(8, 4.5))
    positions = np.arange(len(METRIC_LABELS))
    width = 0.35
    ax.bar(positions - width / 2, rule_heights, width,
           label="规则", color="steelblue")
    ax.bar(positions + width / 2, hybrid_heights, width,
           label="混合", color="seagreen")
    for index, value in enumerate(rule_values):
        if value is None:
            ax.text(positions[index] - width / 2, 0.01, "无数据",
                    ha="center", va="bottom", fontsize=8)
    for index, value in enumerate(hybrid_values):
        if value is None:
            ax.text(positions[index] + width / 2, 0.01, "无数据",
                    ha="center", va="bottom", fontsize=8)
    ax.set_xticks(positions)
    ax.set_xticklabels(["假阳性率", "假阴性率", "精确率", "召回率"])
    ax.set_ylabel("指标值")
    ax.set_ylim(0, 1.05)
    ax.set_title("规则与混合方案测试集指标")
    ax.legend()

    plt.tight_layout()
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "metric_comparison.png")
    plt.savefig(path, dpi=100)
    plt.close(fig)
    return path


def evaluate_all(features_path: str | Path, cache_path: str | Path,
                 config_path: str | Path | None = None) -> dict:
    # 评估主流程：加载配置、模板哈希与缓存，补齐缺失判定后回写特征表，
    # 再输出双口径指标、分组指标、改判明细、缓存覆盖率与对比图
    project_root = Path(__file__).resolve().parent.parent
    features_file = Path(features_path)
    cache_file = Path(cache_path)
    config_file = Path(config_path) if config_path is not None else project_root / "config.json"
    config = json.loads(config_file.read_text(encoding="utf-8"))
    model_cfg = config["model"]

    template_path = project_root / "prompts" / "dga_judge_prompt.md"
    template_hash_value = template_hash(template_path)

    cache = load_cache(cache_file)
    # 只采纳与当前模板哈希、模型、参数、端点一致的有效缓存判定
    cached_verdicts = _verdicts_from_cache(cache, model_cfg, template_hash_value)

    df = pd.read_csv(features_file, dtype={"client_ip": str, "qname": str})
    _validate_features(df)

    judgeable = df[(df["E"] == 1) & (df["B"] == 0)]
    missing_qnames = sorted(set(judgeable["qname"]) - set(cached_verdicts))
    verdicts = dict(cached_verdicts)
    # 可复核但无有效缓存的域名补为 uncertain 并记 no_cache，保留告警而不是丢样本
    for qname in missing_qnames:
        verdicts[qname] = {
            "verdict": "uncertain",
            "reason": "",
            "fail_reason": "no_cache",
        }

    features_sha256 = _sha256_file(features_file)
    # 缓存文件不存在时用空内容的哈希占位，保证字段始终存在
    cache_sha256 = _sha256_file(cache_file) if cache_file.exists() else hashlib.sha256(b"").hexdigest()
    config_sha256 = _sha256_file(config_file)

    out = apply_hybrid(df, verdicts)
    # 复核结果就地回写特征表，后续步骤与下游模块统一读取该文件
    out.to_csv(features_file, index=False)

    splits = ["val", "test"]
    rule_metrics = {}
    hybrid_metrics = {}
    unique_metrics = {}
    reversal = {}
    coverage = {}
    subset_assertions = {}
    for split in splits:
        # 每个划分分别计算规则/混合两套主口径指标，并断言混合阳性是规则阳性的子集
        rule_metrics[split] = compute_metrics(out, "R", split)
        hybrid_metrics[split] = compute_metrics(out, "hybrid", split)
        assert_subset_property(out, out, split)
        subset_assertions[split] = "passed"
        unique_metrics[split] = unique_domain_metrics(out, split)
        reversal[split] = reversal_detail(out, split)
        coverage[split] = _cache_coverage(out, split, cached_verdicts)

    manifest_path = features_file.parent / "run_manifest.json"
    manifest = _read_manifest(manifest_path)
    # 预算抽样标记可能位于 evaluation 子对象或清单顶层，两处任一为真即视为已抽样
    budget_sampling_applied = bool(
        manifest.get("evaluation", {}).get("budget_sampling_applied", False)
        or manifest.get("budget_sampling_applied", False)
    )
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    payload = {
        "generated_at": generated_at,
        "features_sha256": features_sha256,
        "cache_sha256": cache_sha256,
        "config_snapshot_sha256": config_sha256,
        "splits_evaluated": splits,
        "rule": rule_metrics,
        "hybrid": hybrid_metrics,
        "group_metrics": {
            split: grouped_metrics(out, split) for split in splits
        },
        "unique_domain": unique_metrics,
        "reversal_detail": reversal,
        "cache_coverage": coverage,
        "subset_assertions": subset_assertions,
        "budget_sampling_applied": budget_sampling_applied,
    }

    metrics_path = features_file.parent / "metrics.json"
    # 指标统一以 UTF-8、缩进 2、键排序写出，保证 diff 稳定
    metrics_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    figure_path = plot_metric_comparison(
        payload, str(features_file.parent / "figures")
    )

    manifest["evaluation"] = {
        # 把本次评估的输入指纹与覆盖率写回运行清单，便于追溯
        "features_sha256": features_sha256,
        "cache_sha256": cache_sha256,
        "config_snapshot_sha256": config_sha256,
        "splits_evaluated": splits,
        "cache_coverage": coverage,
        "budget_sampling_applied": budget_sampling_applied,
        "generated_at": generated_at,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return {
        "metrics_path": str(metrics_path),
        "figure_path": figure_path,
        "manifest_path": str(manifest_path),
        "splits_evaluated": splits,
        "features_rows": int(len(out)),
        "cache_coverage": coverage,
        "subset_assertions": subset_assertions,
    }


def main(argv: list[str] | None = None) -> int:
    # 命令行入口：默认读取 results 下的特征表与缓存文件
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Hybrid DGA evaluation")
    parser.add_argument(
        "--features", default=str(project_root / "results" / "features.csv")
    )
    parser.add_argument(
        "--cache", default=str(project_root / "results" / "llm_cache.jsonl")
    )
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    try:
        summary = evaluate_all(args.features, args.cache)
    except (OSError, ValueError, KeyError, TypeError, AssertionError) as exc:
        # 预期内的异常只输出错误类型名，返回非零退出码，不打印敏感堆栈
        print(json.dumps({"error": type(exc).__name__}, ensure_ascii=False))
        return 1
    if args.out:
        # 指定 --out 时把指标文件复制到目标路径，并同步摘要中的路径
        source = Path(summary["metrics_path"])
        target = Path(args.out)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        summary["metrics_path"] = str(target)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

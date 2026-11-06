"""一键流水线：日志 → 特征 → 基线 → 规则 → LLM 复核 → 评估。

负责人：组长（D11）
参考：docs/specs/project-roadmap.md 阶段4 D11、7.2 同步点

已实现 D3–D9 全链路；冻结后读取 results/baseline.json 并校验阈值与输入哈希。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from baseline import (
    fit_baseline_from_features,
    load_baseline,
    plot_baseline_histograms,
    save_baseline,
)
from evaluate import evaluate_all
from features import FEATURES_COLUMNS, compute_features, load_log, save_features
from llm_judge import run_judging
from rule_engine import compute_rules, evaluate_candidates
from src.gen_log import run_log_generation
from src.prepare_data import run_data_preparation


STAGE_CHOICES = ["data", "log", "features"]
THRESHOLD_FEATURES = ("H", "F", "I")


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DNS 异常查询检测离线流水线（D3–D9）")
    parser.add_argument("--from", dest="from_stage", choices=STAGE_CHOICES, default="log")
    parser.add_argument("--seed", type=int, default=20261101)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--online", action="store_true")
    mode_group.add_argument("--offline", action="store_true")
    return parser.parse_args(argv)


def _project_paths(root: Path) -> dict[str, Path]:
    return {
        "config": root / "config.json",
        "dns_log": root / "data" / "dns_log.csv",
        "splits": root / "data" / "splits.csv",
        "features": root / "results" / "features.csv",
        "baseline": root / "results" / "baseline.json",
        "figures": root / "results" / "figures",
        "manifest": root / "results" / "run_manifest.json",
        "cache": root / "results" / "llm_cache.jsonl",
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _f1_score(metrics: dict) -> float:
    precision = metrics.get("Precision")
    recall = metrics.get("Recall")
    if precision is None or recall is None:
        return -1.0
    if math.isnan(precision) or math.isnan(recall):
        return -1.0
    if precision + recall == 0:
        return -1.0
    return 2 * precision * recall / (precision + recall)


def _select_quantiles(features_df, candidates: dict) -> tuple[float, float, float]:
    h_values = list(candidates["H_quantile"])
    f_values = list(candidates["F_quantile"])
    i_values = list(candidates["I_quantile"])
    if not h_values or not f_values or not i_values:
        raise ValueError("threshold_candidates must not contain empty lists")
    if len(h_values) == len(f_values) == len(i_values) == 1:
        return h_values[0], f_values[0], i_values[0]
    results = evaluate_candidates(features_df, h_values, f_values, i_values)
    best = max(results, key=_f1_score)
    return best["H_threshold"], best["F_threshold"], best["I_threshold"]


def _read_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("run_manifest.json root must be an object")
    return data


def _merge_manifest(existing: dict, updates: dict, overwrite: bool = True) -> dict:
    merged = dict(existing)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_manifest(merged[key], value, overwrite)
        elif overwrite or key not in merged:
            merged[key] = value
    return merged


def _write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, ensure_ascii=False)


def _validate_frozen_thresholds(baseline: dict, manifest: dict) -> None:
    thresholds = manifest.get("thresholds", {})
    for feature in THRESHOLD_FEATURES:
        expected = thresholds.get(feature)
        actual = baseline["features"][feature]["threshold"]
        if expected is None or actual != expected:
            raise ValueError(
                f"frozen threshold mismatch for {feature}: "
                f"baseline={actual}, manifest={expected}"
            )


def _validate_frozen_inputs(paths: dict[str, Path], manifest: dict) -> None:
    expected = manifest.get("input_sha256", {})
    for key, path in (
        ("data/splits.csv", paths["splits"]),
        ("data/dns_log.csv", paths["dns_log"]),
    ):
        if key not in expected:
            continue
        actual = _sha256_file(path)
        if actual != expected[key]:
            raise ValueError(
                f"{key} hash mismatch: expected {expected[key]}, got {actual}; "
                "reopen the freeze checkpoint before regenerating data"
            )


def _input_hashes(paths: dict[str, Path]) -> dict[str, str]:
    return {
        "config.json": _sha256_file(paths["config"]),
        "data/splits.csv": _sha256_file(paths["splits"]),
        "data/dns_log.csv": _sha256_file(paths["dns_log"]),
    }


def _thresholds_snapshot(baseline: dict) -> dict[str, float]:
    return {
        feature: baseline["features"][feature]["threshold"]
        for feature in THRESHOLD_FEATURES
    }


def main(argv=None, root=None) -> int:
    args = _parse_args(argv)
    mode = "online" if args.online else "offline"
    started_at = _utc_now()
    stages: list[dict] = []
    root_path = Path(root) if root is not None else ROOT
    paths = _project_paths(root_path)

    try:
        with paths["config"].open("r", encoding="utf-8") as stream:
            config = json.load(stream)

        config_seed = config.get("seed")
        if args.seed != config_seed or args.seed != 20261101:
            raise ValueError(
                f"seed mismatch: --seed={args.seed}, config seed={config_seed}, "
                "required 20261101"
            )

        manifest = _read_manifest(paths["manifest"])
        frozen = manifest.get("freeze_check") == "frozen"

        if args.from_stage == "data":
            run_data_preparation(root_path, paths["config"])
            stages.append({"name": "prepare_data", "output": "data/splits.csv"})

        if args.from_stage in ("data", "log"):
            run_log_generation(root_path, paths["config"])
            stages.append({"name": "gen_log", "output": "data/dns_log.csv"})
            if frozen:
                _validate_frozen_inputs(paths, manifest)

        log_df = load_log(str(paths["dns_log"]))
        features_df = compute_features(log_df)
        paths["features"].parent.mkdir(parents=True, exist_ok=True)
        save_features(features_df, str(paths["features"]))
        stages.append({
            "name": "features",
            "output": "results/features.csv",
            "rows": int(len(features_df)),
            "columns": len(FEATURES_COLUMNS),
        })

        if frozen:
            baseline = load_baseline(str(paths["baseline"]))
            _validate_frozen_thresholds(baseline, manifest)
            stages.append({
                "name": "baseline",
                "output": "results/baseline.json",
                "source": "frozen",
                "fitted_on": baseline["fitted_on"],
                "thresholds": _thresholds_snapshot(baseline),
            })
        else:
            h_quantile, f_quantile, i_quantile = _select_quantiles(
                features_df, config["threshold_candidates"]
            )
            baseline = fit_baseline_from_features(
                features_df,
                H_quantile=h_quantile,
                F_quantile=f_quantile,
                I_quantile=i_quantile,
                window_seconds=config["window_seconds"],
            )
            save_baseline(baseline, str(paths["baseline"]))
            plot_baseline_histograms(features_df, baseline, str(paths["figures"]))
            stages.append({
                "name": "baseline",
                "output": "results/baseline.json",
                "source": "fitted",
                "figure": "results/figures/baseline_histograms.png",
                "fitted_on": baseline["fitted_on"],
                "thresholds": _thresholds_snapshot(baseline),
            })

        scored_df = compute_rules(features_df, baseline)
        save_features(scored_df, str(paths["features"]))
        stages.append({
            "name": "rules",
            "output": "results/features.csv",
            "rows": int(len(scored_df)),
            "alerts": int(scored_df["R"].sum()),
        })

        llm_summary = run_judging(
            str(paths["features"]), mode, config_path=paths["config"]
        )
        stages.append({
            "name": "llm",
            "mode": mode,
            "requested_unique": llm_summary["requested_unique"],
            "cache_hits": llm_summary["cache_hits"],
        })

        manifest = _merge_manifest(manifest, {
            "seed": args.seed,
            "mode": mode,
            "stages": stages,
            "input_sha256": _input_hashes(paths),
            "llm": llm_summary,
            "started_at": started_at,
            "finished_at": _utc_now(),
        })
        _write_manifest(paths["manifest"], manifest)

        evaluation_summary = evaluate_all(
            str(paths["features"]), str(paths["cache"]), config_path=paths["config"]
        )
        stages.append({
            "name": "evaluate",
            "metrics": "results/metrics.json",
            "figure": evaluation_summary["figure_path"],
        })
        current = _read_manifest(paths["manifest"])
        current = _merge_manifest(
            current, {"evaluation": evaluation_summary}, overwrite=False
        )
        current = _merge_manifest(current, {
            "stages": stages,
            "finished_at": _utc_now(),
        })
        _write_manifest(paths["manifest"], current)
        print(json.dumps(current, indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"run_all failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

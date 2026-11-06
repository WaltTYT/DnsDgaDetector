import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from src import evaluate, llm_judge
from src.features import FEATURES_COLUMNS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = PROJECT_ROOT / "prompts" / "dga_judge_prompt.md"
ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def _row(qname, label, e, b, r, source="normal_list", algorithm=None,
         split="test", client_ip="10.0.0.1", window_start="2026-11-01T00:00:00Z"):
    return {
        "client_ip": client_ip,
        "window_start": window_start,
        "qname": qname,
        "label": label,
        "source": source,
        "algorithm": algorithm,
        "split": split,
        "H": 1.5,
        "F": 3,
        "I": 10.0,
        "E": e,
        "B": b,
        "R": r,
        "llm_verdict": "",
        "llm_fail_reason": "",
        "hybrid": 0,
    }


def _features(rows):
    return pd.DataFrame(rows, columns=FEATURES_COLUMNS)


def _metrics_df(tp, fp, fn, tn):
    rows = []
    for _ in range(tp):
        rows.append({"label": 1, "pred": 1, "split": "test"})
    for _ in range(fp):
        rows.append({"label": 0, "pred": 1, "split": "test"})
    for _ in range(fn):
        rows.append({"label": 1, "pred": 0, "split": "test"})
    for _ in range(tn):
        rows.append({"label": 0, "pred": 0, "split": "test"})
    return pd.DataFrame(rows)


def _cache_entry(qname, label, verdict, config):
    return llm_judge._cache_entry(
        qname, label, config["model"]["name"],
        llm_judge.sanitize_endpoint(config["model"]["base_url"]),
        llm_judge.template_hash(PROMPT_PATH),
        llm_judge.generation_params(config["model"]), 1, 200,
        json.dumps({"verdict": verdict, "reason": "test"}),
        {"verdict": verdict, "reason": "test"}, 1,
    )


def test_apply_hybrid_truth_table():
    df = _features([
        _row("q0.com", 0, 0, 0, 0),
        _row("q1.com", 0, 1, 0, 1),
        _row("q2.com", 0, 1, 0, 1),
        _row("q3.com", 0, 1, 0, 1),
        _row("q4.com", 1, 1, 0, 1),
        _row("q5.com", 1, 1, 0, 1),
        _row("q6.com", 1, 0, 1, 1),
        _row("q7.com", 0, 1, 1, 1),
    ])
    verdicts = {
        "q1.com": {"verdict": "normal", "fail_reason": None},
        "q2.com": {"verdict": "uncertain", "fail_reason": None},
        "q3.com": {"verdict": "DGA", "fail_reason": None},
        "q4.com": {"verdict": "DGA", "fail_reason": None},
        "q5.com": {"verdict": "normal", "fail_reason": None},
        "q7.com": {"verdict": "normal", "fail_reason": None},
    }
    out = evaluate.apply_hybrid(df, verdicts)
    assert out["llm_verdict"].tolist() == [
        "na", "normal", "uncertain", "DGA", "DGA", "normal", "na", "normal",
    ]
    assert out["hybrid"].tolist() == [0, 0, 1, 1, 1, 0, 1, 1]
    assert out["llm_fail_reason"].tolist() == [""] * 8


def test_apply_hybrid_rejects_fail_with_normal():
    df = _features([_row("q1.com", 0, 1, 0, 1)])
    with pytest.raises(ValueError, match="must be uncertain"):
        evaluate.apply_hybrid(df, {
            "q1.com": {"verdict": "normal", "fail_reason": "timeout"},
        })


def test_compute_metrics_hand_calc():
    df = _metrics_df(tp=8, fp=2, fn=2, tn=18)
    metrics = evaluate.compute_metrics(df, "pred", "test")
    assert (metrics["tp"], metrics["fp"], metrics["fn"], metrics["tn"]) == (8, 2, 2, 18)
    assert metrics["fpr"] == 0.1
    assert metrics["fnr"] == 0.2
    assert metrics["precision"] == 0.8
    assert metrics["recall"] == 0.8
    assert metrics["positive_support"] == 10
    assert metrics["negative_support"] == 20


def test_compute_metrics_zero_denominator():
    negatives = pd.DataFrame({
        "label": [0, 0, 0, 0], "pred": [0, 0, 0, 0], "split": ["test"] * 4,
    })
    metrics = evaluate.compute_metrics(negatives, "pred", "test")
    assert metrics["fpr"] == 0.0
    assert metrics["fnr"] is None
    assert metrics["precision"] is None
    assert metrics["recall"] is None

    positives = pd.DataFrame({
        "label": [1, 1, 1, 1], "pred": [1, 1, 1, 1], "split": ["test"] * 4,
    })
    metrics = evaluate.compute_metrics(positives, "pred", "test")
    assert metrics["fpr"] is None
    assert metrics["fnr"] == 0.0
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 1.0

    empty = negatives[negatives["split"] == "val"]
    metrics = evaluate.compute_metrics(empty, "pred", "test")
    assert metrics["total"] == 0
    assert metrics["fpr"] is None and metrics["precision"] is None


def test_unique_domain_metrics_collapses_by_qname():
    df = _features([
        _row("a.com", 0, 1, 0, 1),
        _row("a.com", 0, 1, 0, 0, client_ip="10.0.0.2"),
        _row("b.com", 1, 1, 0, 1),
    ])
    df = evaluate.apply_hybrid(df, {})
    result = evaluate.unique_domain_metrics(df, "test")
    assert result["unique_domains"] == 2
    assert result["rule"]["tp"] == 1
    assert result["rule"]["fp"] == 1
    assert result["hybrid"]["tp"] == 1
    assert result["hybrid"]["fp"] == 1


def test_assert_subset_property_reports_counterexample_keys():
    rule = _features([
        _row("a.com", 0, 1, 0, 1, window_start="2026-11-01T00:00:00Z"),
        _row("b.com", 0, 1, 0, 1, window_start="2026-11-01T00:00:01Z"),
    ])
    rule = evaluate.apply_hybrid(rule, {
        "a.com": {"verdict": "normal"},
        "b.com": {"verdict": "normal"},
    })
    hybrid = rule.copy()
    hybrid.loc[hybrid["qname"] == "c.com", "hybrid"] = 1
    hybrid = pd.concat([
        hybrid,
        _features([_row("c.com", 0, 0, 0, 0)]).assign(hybrid=1, llm_verdict="DGA"),
    ], ignore_index=True)
    with pytest.raises(AssertionError, match="not subset"):
        evaluate.assert_subset_property(rule, hybrid, "test")


def test_assert_subset_property_dict_mode():
    rule = {"fp": 2, "tp": 8, "tn": 18}
    assert evaluate.assert_subset_property(rule, {"fp": 1, "tp": 7, "tn": 19}, "test")
    with pytest.raises(AssertionError):
        evaluate.assert_subset_property(rule, {"fp": 3, "tp": 8, "tn": 17}, "test")
    with pytest.raises(AssertionError):
        evaluate.assert_subset_property(rule, {"fp": 1, "tp": 9, "tn": 18}, "test")


def test_reversal_detail_counts_and_samples():
    df = _features([
        _row("fp.com", 0, 1, 0, 1),
        _row("wrong.com", 1, 1, 0, 1),
        _row("uncertain.com", 0, 1, 0, 1),
        _row("dga1.com", 0, 1, 0, 1, source="manual_dga", algorithm="word_digit"),
        _row("dga2.com", 1, 1, 0, 1),
        _row("bonly.com", 1, 0, 1, 1),
    ])
    df = evaluate.apply_hybrid(df, {
        "fp.com": {"verdict": "normal"},
        "wrong.com": {"verdict": "normal"},
        "uncertain.com": {"verdict": "uncertain"},
        "dga1.com": {"verdict": "DGA"},
        "dga2.com": {"verdict": "DGA"},
    })
    detail = evaluate.reversal_detail(df, "test")
    assert detail["correct_withdraw_fp"]["count"] == 1
    assert detail["wrong_release_tp"]["count"] == 1
    assert detail["retained_uncertain_or_fail"]["count"] == 1
    assert detail["no_reversal"]["count"] == 2
    manual = detail["no_reversal"]["samples"][0]
    assert manual["qname"] == "dga1.com"
    external = detail["correct_withdraw_fp"]["samples"][0]
    assert "qname" not in external


def test_grouped_metrics_by_source_algorithm_and_rule_hit():
    df = _features([
        _row("n0.com", 0, 0, 0, 0, source="normal_list"),
        _row("n1.com", 0, 1, 0, 1, source="normal_list"),
        _row("m1.com", 1, 1, 0, 1, source="manual_dga", algorithm="rand_alpha"),
        _row("m2.com", 1, 0, 1, 1, source="manual_dga", algorithm="md5_date"),
        _row("p1.com", 1, 1, 0, 1, source="public_dga"),
    ])
    out = evaluate.apply_hybrid(df, {
        "n1.com": {"verdict": "normal"},
        "m1.com": {"verdict": "DGA"},
        "m2.com": {"verdict": "DGA"},
        "p1.com": {"verdict": "DGA"},
    })
    groups = evaluate.grouped_metrics(out, "test")

    assert groups["by_source"]["normal_list"]["rows"] == 2
    assert groups["by_source"]["manual_dga"]["rows"] == 2
    assert groups["by_source"]["public_dga"]["rows"] == 1
    normal = groups["by_source"]["normal_list"]
    assert normal["rule"]["fp"] == 1
    assert normal["rule"]["fpr"] == 0.5
    assert normal["hybrid"]["fp"] == 0
    assert normal["hybrid"]["tn"] == 2
    manual = groups["by_source"]["manual_dga"]
    assert manual["rule"]["tp"] == 2
    assert manual["hybrid"]["tp"] == 2

    assert groups["by_algorithm"]["none"]["rows"] == 3
    assert groups["by_algorithm"]["rand_alpha"]["rows"] == 1
    assert groups["by_algorithm"]["md5_date"]["rows"] == 1

    assert groups["by_rule_hit"]["E_only"]["rows"] == 3
    assert groups["by_rule_hit"]["B_only"]["rows"] == 1
    assert "E_and_B" not in groups["by_rule_hit"]


def test_plot_metric_comparison_writes_png(tmp_path):
    metrics = {
        "rule": {"test": {
            "fpr": 0.1, "fnr": 0.2, "precision": 0.8, "recall": 0.8,
        }},
        "hybrid": {"test": {
            "fpr": 0.05, "fnr": None, "precision": None, "recall": 0.9,
        }},
    }
    path = evaluate.plot_metric_comparison(metrics, str(tmp_path))
    assert Path(path).exists()
    assert Path(path).stat().st_size > 0


def test_evaluate_all_end_to_end(tmp_path):
    config = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    features_path = tmp_path / "features.csv"
    _features([
        _row("d1.com", 1, 1, 0, 1),
        _row("f1.com", 0, 1, 0, 1),
        _row("n1.com", 0, 1, 0, 1),
        _row("d2.com", 1, 0, 1, 1, split="val"),
    ]).to_csv(features_path, index=False)
    features_before = hashlib.sha256(features_path.read_bytes()).hexdigest()

    cache_path = tmp_path / "llm_cache.jsonl"
    with cache_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(_cache_entry("d1.com", "d1", "normal", config)) + "\n")
        handle.write(json.dumps(_cache_entry("f1.com", "f1", "normal", config)) + "\n")
    cache_hash = hashlib.sha256(cache_path.read_bytes()).hexdigest()

    summary = evaluate.evaluate_all(features_path, cache_path, config_path)
    assert summary["subset_assertions"] == {"val": "passed", "test": "passed"}
    assert summary["cache_coverage"]["test"] == {
        "judgeable_unique": 3, "covered": 2, "rate": 0.6667,
    }

    metrics = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["features_sha256"] == features_before
    assert metrics["cache_sha256"] == cache_hash
    assert metrics["rule"]["test"]["tp"] == 1
    assert metrics["rule"]["test"]["fp"] == 2
    assert metrics["rule"]["test"]["fpr"] == 1.0
    assert metrics["rule"]["test"]["precision"] == 0.3333
    assert metrics["rule"]["test"]["recall"] == 1.0
    assert metrics["hybrid"]["test"]["tp"] == 0
    assert metrics["hybrid"]["test"]["fp"] == 1
    assert metrics["hybrid"]["test"]["fn"] == 1
    assert metrics["hybrid"]["test"]["fnr"] == 1.0
    assert metrics["hybrid"]["test"]["precision"] == 0.0
    assert metrics["reversal_detail"]["test"]["correct_withdraw_fp"]["count"] == 1
    assert metrics["reversal_detail"]["test"]["wrong_release_tp"]["count"] == 1
    assert metrics["reversal_detail"]["test"]["retained_uncertain_or_fail"]["count"] == 1
    assert metrics["group_metrics"]["test"]["by_source"]["normal_list"]["rows"] == 3
    assert metrics["group_metrics"]["test"]["by_rule_hit"]["E_only"]["rows"] == 3
    assert metrics["budget_sampling_applied"] is False

    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    evaluation = manifest["evaluation"]
    assert evaluation["features_sha256"] == features_before
    assert evaluation["cache_sha256"] == cache_hash
    assert evaluation["splits_evaluated"] == ["val", "test"]
    assert evaluation["cache_coverage"]["test"]["covered"] == 2

    written = pd.read_csv(features_path, dtype={"client_ip": str})
    row = written[written["qname"] == "n1.com"].iloc[0]
    assert row["llm_verdict"] == "uncertain"
    assert row["llm_fail_reason"] == "no_cache"
    assert int(row["hybrid"]) == 1
    assert list(written.columns) == FEATURES_COLUMNS

    metrics_text = (tmp_path / "metrics.json").read_text(encoding="utf-8")
    for qname in ("d1.com", "f1.com", "n1.com", "d2.com"):
        assert qname not in metrics_text


def test_evaluate_main_cli(tmp_path, capsys):
    config = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    features_path = tmp_path / "features.csv"
    _features([_row("a.com", 0, 0, 0, 0, split="val")]).to_csv(features_path, index=False)
    cache_path = tmp_path / "llm_cache.jsonl"
    cache_path.write_text("", encoding="utf-8")

    assert evaluate.main(["--features", str(features_path), "--cache", str(cache_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["splits_evaluated"] == ["val", "test"]
    assert payload["subset_assertions"] == {"val": "passed", "test": "passed"}

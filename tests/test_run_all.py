import json
from pathlib import Path

import pandas as pd
import pytest

import run_all
from src.features import FEATURES_COLUMNS

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _frozen_manifest():
    return {
        "freeze_check": "frozen",
        "frozen_at": "2026-11-06T01:26:16.494Z",
        "thresholds": {
            "H": 3.5369,
            "F": 12.0,
            "I": 1.7587,
            "H_quantile": 0.99,
            "F_quantile": 0.99,
            "I_quantile": 0.01,
            "fitted_on": {"split": "train", "label": 0},
        },
        "model": {"name": "GLM-4-Flash", "temperature": 0},
        "prompt_template_hash": "template-hash",
        "validation": {"rule": {"tp": 1, "fp": 0}},
        "input_sha256": {"config.json": "config-hash"},
        "evaluation": {
            "cache_sha256": "old-cache-hash",
            "cache_coverage": {
                "test": {"covered": 1, "judgeable_unique": 1, "rate": 1.0},
            },
        },
    }


def _baseline():
    return {
        "version": 1,
        "window_seconds": 60,
        "fitted_on": {"split": "train", "label": 0},
        "fitted_at": "2026-11-06T01:26:12.698Z",
        "python_version": "3.11.17",
        "numpy_version": "2.4.6",
        "pandas_version": "3.0.6",
        "psl_source": "tldextract==5.4.0 built-in snapshot",
        "features": {
            "H": {
                "quantile": 0.99, "threshold": 3.5369,
                "method": "numpy.percentile(q, method='linear')", "comparator": ">",
            },
            "F": {
                "quantile": 0.99, "threshold": 12.0,
                "method": "numpy.percentile(q, method='linear')", "comparator": ">",
            },
            "I": {
                "quantile": 0.01, "threshold": 1.7587,
                "method": "numpy.percentile(q, method='linear')", "comparator": "<",
            },
        },
        "freeze_check": "not frozen until D7",
    }


def _features_df():
    rows = [
        {
            "client_ip": "10.0.0.1",
            "window_start": "2026-11-01T00:00:00Z",
            "qname": "a.com",
            "label": 0,
            "source": "normal_list",
            "algorithm": None,
            "split": "test",
            "H": 1.0,
            "F": 1,
            "I": 5.0,
            "E": 1,
            "B": 0,
            "R": 1,
            "llm_verdict": "na",
            "llm_fail_reason": "",
            "hybrid": 0,
        },
        {
            "client_ip": "10.0.0.2",
            "window_start": "2026-11-01T00:01:00Z",
            "qname": "b.com",
            "label": 1,
            "source": "manual_dga",
            "algorithm": "rand_alpha",
            "split": "val",
            "H": 4.0,
            "F": 1,
            "I": 5.0,
            "E": 0,
            "B": 0,
            "R": 0,
            "llm_verdict": "na",
            "llm_fail_reason": "",
            "hybrid": 0,
        },
    ]
    return pd.DataFrame(rows, columns=FEATURES_COLUMNS)


def _prepare_root(tmp_path):
    root = tmp_path
    (root / "results").mkdir()
    (root / "data").mkdir()
    config = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (root / "data" / "dns_log.csv").write_text("placeholder\n", encoding="utf-8")
    (root / "data" / "splits.csv").write_text("placeholder\n", encoding="utf-8")
    (root / "results" / "run_manifest.json").write_text(
        json.dumps(_frozen_manifest()), encoding="utf-8"
    )
    (root / "results" / "baseline.json").write_text(
        json.dumps(_baseline()), encoding="utf-8"
    )
    return root


def test_merge_manifest_preserves_frozen_fields():
    existing = _frozen_manifest()
    updates = {
        "mode": "offline",
        "stages": [{"name": "features"}],
        "llm": {"mode": "offline", "cache_hits": 2},
        "evaluation": {"features_rows": 2},
        "finished_at": "2026-11-06T02:00:00Z",
    }
    merged = run_all._merge_manifest(existing, updates)
    assert merged["freeze_check"] == "frozen"
    assert merged["thresholds"] == existing["thresholds"]
    assert merged["model"] == existing["model"]
    assert merged["prompt_template_hash"] == "template-hash"
    assert merged["validation"] == existing["validation"]
    assert merged["mode"] == "offline"
    assert merged["stages"] == [{"name": "features"}]
    assert merged["llm"] == {"mode": "offline", "cache_hits": 2}


@pytest.mark.parametrize("case", ["offline", "cache_missing", "existing_cache"])
def test_run_all_offline_paths(tmp_path, monkeypatch, case):
    root = _prepare_root(tmp_path)
    cache_path = root / "results" / "llm_cache.jsonl"
    if case == "cache_missing":
        cache_path.write_text("", encoding="utf-8")
    elif case == "existing_cache":
        cache_path.write_text('{"cache_key": "k"}\n', encoding="utf-8")

    cache_missing = case == "cache_missing"
    llm_summary = {
        "mode": "offline",
        "requested_unique": 3 if cache_missing else 2,
        "cache_hits": 0 if cache_missing else 2,
        "verdict_counts": (
            {"DGA": 0, "normal": 0, "uncertain": 3}
            if cache_missing
            else {"DGA": 1, "normal": 1, "uncertain": 0}
        ),
        "fail_reason_counts": {"no_cache": 3} if cache_missing else {},
        "offline_coverage": 0.0 if cache_missing else 1.0,
        "budget_applied": False,
        "sampling_rule": None,
    }
    calls = {}

    def fake_judging(features_path, mode, config_path=None):
        calls["mode"] = mode
        calls["features_path"] = Path(features_path)
        calls["config_path"] = Path(config_path)
        return dict(llm_summary)

    written_coverage = {
        "test": {
            "covered": 0 if cache_missing else 2,
            "judgeable_unique": 2,
            "rate": 0.0 if cache_missing else 1.0,
        },
    }

    def fake_evaluate(features_path, cache_path, config_path=None):
        manifest_path = Path(features_path).parent / "run_manifest.json"
        current = json.loads(manifest_path.read_text(encoding="utf-8"))
        current["evaluation"] = {
            "cache_sha256": "evaluated-cache-hash",
            "cache_coverage": written_coverage,
            "splits_evaluated": ["val", "test"],
        }
        manifest_path.write_text(json.dumps(current), encoding="utf-8")
        calls["evaluated"] = True
        return {
            "metrics_path": "results/metrics.json",
            "figure_path": "results/figures/metric_comparison.png",
            "manifest_path": str(manifest_path),
            "splits_evaluated": ["val", "test"],
            "features_rows": 2,
            "cache_coverage": {
                "test": {"covered": 9, "judgeable_unique": 9, "rate": 1.0},
            },
            "subset_assertions": {"val": "passed", "test": "passed"},
        }

    monkeypatch.setattr(run_all, "load_log", lambda path: pd.DataFrame({"x": [1]}))
    monkeypatch.setattr(run_all, "compute_features", lambda log_df: _features_df())
    monkeypatch.setattr(
        run_all, "compute_rules",
        lambda df, baseline: df.assign(E=1, B=0, R=1),
    )
    monkeypatch.setattr(run_all, "run_judging", fake_judging)
    monkeypatch.setattr(run_all, "evaluate_all", fake_evaluate)

    assert run_all.main(["--from", "features", "--offline"], root=root) == 0

    manifest = json.loads((root / "results" / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["freeze_check"] == "frozen"
    assert manifest["thresholds"] == _frozen_manifest()["thresholds"]
    assert manifest["mode"] == "offline"
    assert manifest["llm"] == llm_summary
    assert [stage["name"] for stage in manifest["stages"]] == [
        "features", "baseline", "rules", "llm", "evaluate",
    ]
    assert manifest["stages"][1]["source"] == "frozen"
    assert calls["mode"] == "offline"
    assert calls["features_path"] == root / "results" / "features.csv"
    assert calls["config_path"] == root / "config.json"
    assert calls["evaluated"] is True
    assert manifest["evaluation"]["cache_sha256"] == "evaluated-cache-hash"
    assert manifest["evaluation"]["cache_coverage"] == written_coverage
    assert manifest["evaluation"]["features_rows"] == 2
    assert manifest["evaluation"]["subset_assertions"] == {
        "val": "passed", "test": "passed",
    }


def test_seed_mismatch_returns_nonzero(tmp_path, capsys):
    root = _prepare_root(tmp_path)
    assert run_all.main(["--from", "features", "--offline", "--seed", "1"], root=root) == 1
    assert "seed mismatch" in capsys.readouterr().err

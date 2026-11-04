"""特征单元测试（组员B，D5–D6）。

覆盖：香农熵计算、固定 PSL 后缀提取（example.co.uk → example）、
窗口边界（UTC 整分钟对齐）、间隔缺失（<3 次查询）、非法标签与 IP 字面量处理、
混合窗口 F/I 关联、基线拟合、baseline.json schema、阈值比较运算符。
"""

import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from features import (
    DNS_LOG_COLUMNS,
    FEATURES_COLUMNS,
    align_to_window_start,
    compute_features,
    extract_label,
    normalize_qname,
    shannon_entropy,
)
from baseline import (
    BASELINE_SCHEMA_VERSION,
    fit_baseline_from_features,
    load_baseline,
    save_baseline,
)
from rule_engine import (
    compute_rules,
    evaluate_candidates,
    summarize_client_contamination,
)


ENTROPY_CASES = [
    ("hello", 1.9219),
    ("aaa", 0.0),
    ("ab", 1.0),
    ("abc", round(-(math.log2(1/3)), 4)),
    ("", float("nan")),
    ("a", 0.0),
    ("aab", round(-(2/3)*math.log2(2/3) - (1/3)*math.log2(1/3), 4)),
]


class TestShannonEntropy:
    @pytest.mark.parametrize("s,expected", ENTROPY_CASES)
    def test_entropy_values(self, s, expected):
        result = shannon_entropy(s)
        if math.isnan(expected):
            assert math.isnan(result), f"{s!r}: expected NaN, got {result}"
        else:
            assert abs(result - expected) < 0.001, f"{s!r}: expected {expected}, got {result}"

    def test_equal_chars_max_entropy(self):
        s = "abcdefghij"
        h = shannon_entropy(s)
        assert abs(h - math.log2(10)) < 0.001

    def test_empty_string(self):
        assert math.isnan(shannon_entropy(""))


PSL_CASES = [
    ("mail.cctv.com", "cctv"),
    ("sub.example.co.uk", "example"),
    ("github.com", "github"),
    ("a.b.c.example.com", "example"),
    ("example", "example"),
    ("sub-123.example.org", "example"),
]


class TestPslExtraction:
    @pytest.mark.parametrize("qname,expected_label", PSL_CASES)
    def test_extract_label(self, qname, expected_label):
        label, status = extract_label(qname)
        assert label == expected_label, f"{qname!r}: expected {expected_label}, got {label}"
        assert status == "ok"

    def test_ip_literal(self):
        label, status = extract_label("192.168.1.1")
        assert label == ""
        assert status == "ip_literal"

    def test_punycode(self):
        label, status = extract_label("xn--bcher-kva.ch")
        assert label == ""
        assert status == "punycode"

    def test_empty(self):
        label, status = extract_label("")
        assert label == ""
        assert status == "empty"

    def test_no_domain(self):
        label, status = extract_label(".")
        assert label == ""
        assert status == "no_domain"

    def test_tldextract_offline(self):
        import tldextract
        ext = tldextract.TLDExtract(
            include_psl_private_domains=False,
            cache_dir=None,
            fallback_to_snapshot=False,
        )
        result = ext("sub.example.co.uk")
        assert result.domain == "example"
        assert result.suffix == "co.uk"


class TestQnameNormalization:
    def test_lowercase(self):
        assert normalize_qname("WWW.Example.COM") == "www.example.com"

    def test_trailing_dot(self):
        assert normalize_qname("www.example.com.") == "www.example.com"

    def test_whitespace(self):
        assert normalize_qname("  www.example.com  ") == "www.example.com"

    def test_empty(self):
        assert normalize_qname("") == ""


WINDOW_CASES = [
    ("2026-11-01T12:05:45.123Z", "2026-11-01T12:05:00Z"),
    ("2026-11-01T12:05:59.999Z", "2026-11-01T12:05:00Z"),
    ("2026-11-01T12:05:00.000Z", "2026-11-01T12:05:00Z"),
    ("2026-11-01T12:06:00.000Z", "2026-11-01T12:06:00Z"),
    ("2026-11-01T00:00:00.000Z", "2026-11-01T00:00:00Z"),
    ("2026-12-31T23:59:59.999Z", "2026-12-31T23:59:00Z"),
]


class TestWindowAlignment:
    @pytest.mark.parametrize("ts,expected", WINDOW_CASES)
    def test_window_start(self, ts, expected):
        result = align_to_window_start(ts)
        assert result == expected, f"{ts}: expected {expected}, got {result}"


def _make_log_df(rows):
    df = pd.DataFrame(rows, columns=DNS_LOG_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


class TestComputeFeatures:
    def _build_log(self, events):
        rows = []
        for i, ev in enumerate(events, start=1):
            rows.append([
                i, ev["ts"], ev["client_ip"], ev["qname"], ev.get("qtype", "A"),
                ev.get("label", 0), ev.get("source", "normal_list"),
                ev.get("algorithm", ""), ev.get("split", "train"),
            ])
        return _make_log_df(rows)

    def test_interval_less_than_3_is_nan(self):
        events = [
            {"ts": "2026-11-01T12:05:00.000Z", "client_ip": "10.0.0.1", "qname": "www.example.com", "label": 0, "split": "train"},
            {"ts": "2026-11-01T12:05:01.000Z", "client_ip": "10.0.0.1", "qname": "www.example.com", "label": 0, "split": "train"},
        ]
        log_df = self._build_log(events)
        features = compute_features(log_df)
        assert features["I"].isna().all(), "I should be NaN when < 3 queries in window"
        assert features["F"].iloc[0] == 2

    def test_interval_median(self):
        events = [
            {"ts": "2026-11-01T12:05:00.000Z", "client_ip": "10.0.0.1", "qname": "www.example.com", "label": 0, "split": "train"},
            {"ts": "2026-11-01T12:05:04.000Z", "client_ip": "10.0.0.1", "qname": "www.example.com", "label": 0, "split": "train"},
            {"ts": "2026-11-01T12:05:10.000Z", "client_ip": "10.0.0.1", "qname": "www.example.com", "label": 0, "split": "train"},
        ]
        log_df = self._build_log(events)
        features = compute_features(log_df)
        assert features["F"].iloc[0] == 3
        interval = features["I"].iloc[0]
        assert interval is not None and not pd.isna(interval)
        assert abs(interval - 5.0) < 0.01

    def test_f_and_i_shared_across_qnames_same_window(self):
        events = [
            {"ts": "2026-11-01T12:05:00.000Z", "client_ip": "10.0.0.1", "qname": "www.example.com", "label": 0, "split": "train"},
            {"ts": "2026-11-01T12:05:04.000Z", "client_ip": "10.0.0.1", "qname": "www.google.com", "label": 0, "split": "train"},
            {"ts": "2026-11-01T12:05:10.000Z", "client_ip": "10.0.0.1", "qname": "www.baidu.com", "label": 0, "split": "train"},
        ]
        log_df = self._build_log(events)
        features = compute_features(log_df)
        assert len(features) == 3
        assert (features["F"] == 3).all()
        interval_val = features["I"].iloc[0]
        assert (features["I"].fillna(-1) == interval_val).all()

    def test_features_columns_contract(self):
        events = [
            {"ts": "2026-11-01T12:05:00.000Z", "client_ip": "10.0.0.1", "qname": "www.example.com", "label": 0, "split": "train"},
        ]
        log_df = self._build_log(events)
        features = compute_features(log_df)
        assert list(features.columns) == FEATURES_COLUMNS

    def test_entropy_computed_from_psl_label(self):
        events = [
            {"ts": "2026-11-01T12:05:00.000Z", "client_ip": "10.0.0.1", "qname": "mail.cctv.com", "label": 0, "split": "train"},
            {"ts": "2026-11-01T12:05:00.100Z", "client_ip": "10.0.0.1", "qname": "sub.example.co.uk", "label": 0, "split": "train"},
        ]
        log_df = self._build_log(events)
        features = compute_features(log_df)

        row_cctv = features[features["qname"] == "mail.cctv.com"].iloc[0]
        assert row_cctv["H"] == shannon_entropy("cctv")

        row_example = features[features["qname"] == "sub.example.co.uk"].iloc[0]
        assert row_example["H"] == shannon_entropy("example")

    def test_window_boundary(self):
        events = [
            {"ts": "2026-11-01T12:05:59.000Z", "client_ip": "10.0.0.1", "qname": "a.com", "label": 0, "split": "train"},
            {"ts": "2026-11-01T12:06:01.000Z", "client_ip": "10.0.0.1", "qname": "b.com", "label": 0, "split": "train"},
        ]
        log_df = self._build_log(events)
        features = compute_features(log_df)
        windows = features["window_start"].unique()
        assert len(windows) == 2, f"should be 2 distinct windows, got {windows.tolist()}"

    def test_log_columns_validation(self, tmp_path):
        bad = pd.DataFrame([[1, "2026-11-01T12:05:00Z", "10.0.0.1", "x.com", "A"]],
                          columns=["event_id", "timestamp", "client_ip", "qname", "qtype"])
        bad_path = tmp_path / "bad_log.csv"
        bad.to_csv(bad_path, index=False)
        from features import load_log
        with pytest.raises(ValueError):
            load_log(str(bad_path))


class TestBaselineFitting:
    def _make_features_df(self):
        rows = []
        c = 0
        for split in ["train", "val", "test"]:
            for label in [0, 1]:
                for i in range(30):
                    rows.append({
                        "client_ip": f"10.0.{c}.1",
                        "window_start": f"2026-11-01T12:0{i%10}:00Z",
                        "qname": f"domain{i}_{split}_{label}.com",
                        "label": label,
                        "source": "manual_dga" if label else "normal_list",
                        "algorithm": "rand_alpha" if label else "",
                        "split": split,
                        "H": float(np.random.uniform(1.5, 4.0)),
                        "F": float(np.random.randint(1, 50)),
                        "I": float(np.random.uniform(0.5, 15)) if i % 3 != 0 else np.nan,
                        "E": pd.NA, "B": pd.NA, "R": pd.NA,
                        "llm_verdict": pd.NA, "llm_fail_reason": pd.NA, "hybrid": pd.NA,
                    })
                    c += 1
        return pd.DataFrame(rows, columns=FEATURES_COLUMNS)

    def test_baseline_only_uses_train_label0(self):
        np.random.seed(42)
        df = self._make_features_df()
        baseline = fit_baseline_from_features(df)
        assert baseline["fitted_on"] == {"split": "train", "label": 0}

    def test_baseline_version(self):
        np.random.seed(42)
        df = self._make_features_df()
        baseline = fit_baseline_from_features(df)
        assert baseline["version"] == BASELINE_SCHEMA_VERSION

    def test_baseline_has_all_features(self):
        np.random.seed(42)
        df = self._make_features_df()
        baseline = fit_baseline_from_features(df)
        for feat in ["H", "F", "I"]:
            assert feat in baseline["features"]
            inner = baseline["features"][feat]
            for key in ["quantile", "threshold", "method", "comparator"]:
                assert key in inner, f"features.{feat} missing {key}"

    def test_baseline_comparators(self):
        np.random.seed(42)
        df = self._make_features_df()
        baseline = fit_baseline_from_features(df)
        assert baseline["features"]["H"]["comparator"] == ">"
        assert baseline["features"]["F"]["comparator"] == ">"
        assert baseline["features"]["I"]["comparator"] == "<"

    def test_baseline_save_and_load(self, tmp_path):
        np.random.seed(42)
        df = self._make_features_df()
        baseline = fit_baseline_from_features(df)
        path = str(tmp_path / "baseline.json")
        save_baseline(baseline, path)
        loaded = load_baseline(path)
        assert loaded["features"]["H"]["threshold"] == baseline["features"]["H"]["threshold"]
        assert loaded["features"]["F"]["threshold"] == baseline["features"]["F"]["threshold"]
        assert loaded["features"]["I"]["threshold"] == baseline["features"]["I"]["threshold"]

    def test_baseline_load_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_baseline(str(tmp_path / "nonexistent.json"))

    def test_baseline_no_train_normal(self):
        df = pd.DataFrame([{
            "client_ip": "10.0.0.1", "window_start": "2026-11-01T12:05:00Z",
            "qname": "dga.com", "label": 1, "source": "manual_dga", "algorithm": "rand_alpha",
            "split": "val", "H": 3.5, "F": 10.0, "I": 5.0,
            "E": pd.NA, "B": pd.NA, "R": pd.NA,
            "llm_verdict": pd.NA, "llm_fail_reason": pd.NA, "hybrid": pd.NA,
        }], columns=FEATURES_COLUMNS)
        with pytest.raises(ValueError, match="No train\+label=0"):
            fit_baseline_from_features(df)


class TestRuleEngine:
    def _make_features_df(self):
        rows = []
        np.random.seed(123)
        idx = 0
        for split in ["train", "val", "test"]:
            for label in [0, 1]:
                for i in range(20):
                    h = float(np.random.uniform(1.5, 4.5))
                    f = float(np.random.randint(1, 50))
                    i_val = float(np.random.uniform(0.5, 15)) if i % 4 != 0 else np.nan
                    rows.append({
                        "client_ip": f"10.0.{idx}.1",
                        "window_start": f"2026-11-01T12:{idx % 60:02d}:00Z",
                        "qname": f"d{idx}_{split}_{label}.com",
                        "label": label,
                        "source": "manual_dga" if label else "normal_list",
                        "algorithm": "rand_alpha" if label else "",
                        "split": split,
                        "H": h, "F": f, "I": i_val,
                        "E": pd.NA, "B": pd.NA, "R": pd.NA,
                        "llm_verdict": pd.NA, "llm_fail_reason": pd.NA, "hybrid": pd.NA,
                    })
                    idx += 1
        return pd.DataFrame(rows, columns=FEATURES_COLUMNS)

    def test_manual_rule_calculation(self):
        df = self._make_features_df()
        baseline = fit_baseline_from_features(df)
        scored = compute_rules(df, baseline)

        h_thr = baseline["features"]["H"]["threshold"]
        f_thr = baseline["features"]["F"]["threshold"]
        i_thr = baseline["features"]["I"]["threshold"]

        for _, row in scored.iterrows():
            expected_e = 1 if (pd.notna(row["H"]) and row["H"] > h_thr) else 0
            freq = 1 if (pd.notna(row["F"]) and row["F"] > f_thr) else 0
            interval = 1 if (pd.notna(row["I"]) and row["I"] < i_thr) else 0
            expected_b = 1 if (freq == 1 or interval == 1) else 0
            expected_r = 1 if (expected_e == 1 or expected_b == 1) else 0

            assert int(row["E"]) == expected_e
            assert int(row["B"]) == expected_b
            assert int(row["R"]) == expected_r

    def test_interval_missing_skips_interval_rule(self):
        df = self._make_features_df()
        baseline = fit_baseline_from_features(df)

        df_copy = df.copy()
        df_copy["I"] = np.nan

        scored = compute_rules(df_copy, baseline)

        i_thr = baseline["features"]["I"]["threshold"]
        f_thr = baseline["features"]["F"]["threshold"]
        assert (scored["F"].notna()).all()
        expected_b_from_freq = (scored["F"] > f_thr).astype(int)
        assert (scored["B"].values == expected_b_from_freq.values).all()

    def test_never_touch_test_in_candidates(self):
        df = self._make_features_df()
        baseline = fit_baseline_from_features(df)

        h_q = baseline["features"]["H"]["threshold"]
        f_q = baseline["features"]["F"]["threshold"]
        i_q = baseline["features"]["I"]["threshold"]

        results = evaluate_candidates(df, [h_q], [f_q], [i_q])
        assert len(results) == 1
        assert "TP" in results[0]
        assert "FPR" in results[0]

        val_only = df[df["split"] == "val"].copy()
        scored_v = compute_rules(val_only, baseline)
        manual_tp = int(((scored_v["label"] == 1) & (scored_v["R"] == 1)).sum())
        assert results[0]["TP"] == manual_tp

    def test_comparator_strict_not_ge(self):
        df = self._make_features_df()
        baseline = fit_baseline_from_features(df)

        h_thr = baseline["features"]["H"]["threshold"]

        edge_case = pd.DataFrame([{
            "client_ip": "10.0.0.1", "window_start": "2026-11-01T12:05:00Z",
            "qname": "edge.com", "label": 1, "source": "manual_dga", "algorithm": "rand_alpha",
            "split": "test", "H": h_thr, "F": 5.0, "I": 5.0,
            "E": pd.NA, "B": pd.NA, "R": pd.NA,
            "llm_verdict": pd.NA, "llm_fail_reason": pd.NA, "hybrid": pd.NA,
        }], columns=FEATURES_COLUMNS)

        temp_baseline = {
            "version": BASELINE_SCHEMA_VERSION,
            "features": {
                "H": {"threshold": h_thr},
                "F": {"threshold": 100.0},
                "I": {"threshold": 0.5},
            }
        }
        scored = compute_rules(edge_case, temp_baseline)
        assert int(scored["E"].iloc[0]) == 0, f"H equal to threshold should NOT trigger E=1 (strict >). H={h_thr}"

        edge_case2 = pd.DataFrame([{
            "client_ip": "10.0.0.1", "window_start": "2026-11-01T12:05:00Z",
            "qname": "edge2.com", "label": 1, "source": "manual_dga", "algorithm": "rand_alpha",
            "split": "test", "H": h_thr + 0.001, "F": 5.0, "I": 5.0,
            "E": pd.NA, "B": pd.NA, "R": pd.NA,
            "llm_verdict": pd.NA, "llm_fail_reason": pd.NA, "hybrid": pd.NA,
        }], columns=FEATURES_COLUMNS)
        scored2 = compute_rules(edge_case2, temp_baseline)

    def test_contamination_summary(self):
        df = self._make_features_df()
        baseline = fit_baseline_from_features(df)
        summary = summarize_client_contamination(df, baseline)
        assert "overall" in summary
        assert "by_split" in summary
        assert "note" in summary
        for split in ["train", "val", "test"]:
            if split in summary["by_split"]:
                stats = summary["by_split"][split]
                assert "normal_B_hit_rate" in stats
                assert 0.0 <= stats["normal_B_hit_rate"] <= 1.0

    def test_baseline_missing_or_corrupt(self, tmp_path):
        path = str(tmp_path / "missing.json")
        with pytest.raises(FileNotFoundError):
            load_baseline(path)


class TestDryRunIntegration:
    def test_dns_demo_dry_run(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "src.dns_demo", "--dry-run"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "DRY RUN" in result.stdout
        assert "未发送任何网络请求" in result.stdout

import json
from pathlib import Path

import pandas as pd
import pytest

from src import llm_judge
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


def _cfg(tmp_path, max_retries=2, cache_name="cache.jsonl"):
    return {
        "model": "GLM-4-Flash",
        "endpoint": ENDPOINT,
        "timeout": 30,
        "max_retries": max_retries,
        "temperature": 0,
        "max_tokens": 32,
        "template": llm_judge.load_template(PROMPT_PATH),
        "template_hash": llm_judge.template_hash(PROMPT_PATH),
        "cache_path": tmp_path / cache_name,
    }


def _entry(qname, registrable_label, cfg, verdict="normal", reason="ok"):
    return llm_judge._cache_entry(
        qname, registrable_label, cfg["model"],
        llm_judge.sanitize_endpoint(cfg["endpoint"]), cfg["template_hash"],
        llm_judge.generation_params(cfg), 1, 200,
        json.dumps({"verdict": verdict, "reason": reason}),
        {"verdict": verdict, "reason": reason}, 10,
    )


def test_template_hash_matches_header():
    recorded = __import__("re").search(
        r"(?m)^- template_hash: (\S+)$",
        PROMPT_PATH.read_text(encoding="utf-8"),
    ).group(1)
    assert llm_judge.template_hash(PROMPT_PATH) == recorded


def test_select_judgeable_filters_and_dedupes():
    df = _features([
        _row("q1.example", 1, 1, 0, 1, source="manual_dga", algorithm="word_digit"),
        _row("q1.example", 1, 1, 0, 1, source="manual_dga", algorithm="word_digit"),
        _row("q2.example", 0, 1, 1, 1),
        _row("q3.example", 0, 0, 0, 0),
    ])
    selected = llm_judge.select_judgeable(df)
    assert selected["qname"].tolist() == ["q1.example"]


def test_select_judgeable_rejects_bad_columns():
    df = _features([_row("q1.example", 1, 1, 0, 1)]).drop(columns=["llm_fail_reason"])
    with pytest.raises(ValueError, match="features columns mismatch"):
        llm_judge.select_judgeable(df)


def test_build_prompt_renders_without_leakage():
    template = llm_judge.load_template(PROMPT_PATH)
    prompt = llm_judge.build_prompt("xkqzmtvp8f.net", "xkqzmtvp8f", template)
    assert "xkqzmtvp8f.net" in prompt
    assert "xkqzmtvp8f" in prompt
    assert "{{" not in prompt and "}}" not in prompt
    for token in ("source=", "algorithm=", "split="):
        assert token not in prompt


def test_build_prompt_rejects_unknown_variable_and_metadata():
    with pytest.raises(ValueError, match="unknown template variables"):
        llm_judge.build_prompt("a.com", "a", "{{qname}} {{unknown}}")
    with pytest.raises(ValueError, match="metadata token leaked"):
        llm_judge.build_prompt("a.com", "a", "{{qname}} source=manual")
    with pytest.raises(ValueError):
        llm_judge.build_prompt("a.com\nb.com", "a", "{{qname}}")


def test_parse_verdict_accepts_fence_and_truncates():
    verdict, reason = llm_judge.parse_verdict(
        '```json\n{"verdict": "DGA", "reason": "' + "x" * 130 + '"}\n```'
    )
    assert verdict == "DGA"
    assert len(reason) == 120
    assert llm_judge.parse_verdict('{"verdict":"uncertain","reason":"低置信"}') == (
        "uncertain", "低置信"
    )


@pytest.mark.parametrize("raw", [
    "not json",
    '{"verdict":"DGA"}',
    '{"verdict":"DGA","reason":"ok","extra":1}',
    '{"verdict":"bad","reason":"ok"}',
    '{"verdict":"DGA","reason":""}',
])
def test_parse_verdict_rejects_invalid(raw):
    with pytest.raises(ValueError):
        llm_judge.parse_verdict(raw)


def test_cache_key_is_deterministic_and_param_sensitive():
    base = dict(qname="a.com", registrable_label="a", model="GLM-4-Flash",
                template_hash_value="abc", params={"temperature": 0, "max_tokens": 32},
                endpoint=ENDPOINT)
    assert llm_judge.cache_key(**base) == llm_judge.cache_key(**base)
    changed = dict(base, params={"temperature": 0.2, "max_tokens": 32})
    assert llm_judge.cache_key(**base) != llm_judge.cache_key(**changed)


def test_cache_roundtrip_and_no_duplicate(tmp_path):
    cfg = _cfg(tmp_path)
    entry = _entry("a.com", "a", cfg)
    llm_judge.append_cache(entry, cfg["cache_path"])
    llm_judge.append_cache(entry, cfg["cache_path"])
    cache = llm_judge.load_cache(cfg["cache_path"])
    assert len(cache) == 1
    lines = cfg["cache_path"].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert cache[entry["cache_key"]]["parsed"]["verdict"] == "normal"


def test_append_cache_rejects_bad_fields(tmp_path):
    bad = {"cache_key": "k"}
    with pytest.raises(ValueError, match="fields mismatch"):
        llm_judge.append_cache(bad, tmp_path / "cache.jsonl")


def test_load_cache_rejects_duplicate_keys(tmp_path):
    cfg = _cfg(tmp_path)
    entry = _entry("a.com", "a", cfg)
    path = cfg["cache_path"]
    path.write_text(
        json.dumps(entry, ensure_ascii=False) + "\n" + json.dumps(entry, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate cache_key"):
        llm_judge.load_cache(path)


def test_judge_domain_offline_never_calls_api(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        llm_judge.requests, "post",
        lambda *args, **kwargs: pytest.fail("offline mode must not open HTTP"),
    )
    result = llm_judge.judge_domain("a.com", "a", "offline", cfg)
    assert result.verdict == "uncertain"
    assert result.fail_reason == "no_cache"
    assert not result.cache_hit


def test_judge_domain_online_retries_and_caches(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, max_retries=2)
    calls = []
    sleeps = []

    def fake_call(prompt, cfg):
        calls.append(prompt)
        if len(calls) < 3:
            raise llm_judge.LLMCallError("timeout")
        return '{"verdict":"DGA","reason":"随机标签"}', 5

    monkeypatch.setattr(llm_judge, "call_llm", fake_call)
    monkeypatch.setattr(llm_judge, "_sleep", lambda seconds: sleeps.append(seconds))
    result = llm_judge.judge_domain("a.com", "a", "online", cfg)
    assert result.verdict == "DGA"
    assert len(calls) == 3
    assert sleeps == [1, 2]
    cache = llm_judge.load_cache(cfg["cache_path"])
    entry = next(iter(cache.values()))
    assert entry["attempt_count"] == 3
    assert entry["http_status"] == 200
    assert entry["parsed"]["verdict"] == "DGA"


def test_judge_domain_format_error_is_not_retried(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, max_retries=3)
    calls = []

    def fake_call(prompt, cfg):
        calls.append(prompt)
        return "not json", 1

    monkeypatch.setattr(llm_judge, "call_llm", fake_call)
    result = llm_judge.judge_domain("a.com", "a", "online", cfg)
    assert result.fail_reason == "format_error"
    assert len(calls) == 1
    assert not cfg["cache_path"].exists()


def test_judge_domain_auth_error_is_not_retried(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, max_retries=3)
    calls = []

    def fake_call(prompt, cfg):
        calls.append(prompt)
        raise llm_judge.LLMCallError("auth_error")

    monkeypatch.setattr(llm_judge, "call_llm", fake_call)
    result = llm_judge.judge_domain("a.com", "a", "online", cfg)
    assert result.fail_reason == "auth_error"
    assert len(calls) == 1


@pytest.mark.parametrize("category", ["timeout", "rate_limit", "server_error"])
def test_judge_domain_retryable_failure_exhausts_budget(tmp_path, monkeypatch, category):
    cfg = _cfg(tmp_path, max_retries=2)
    calls = []
    sleeps = []

    def fake_call(prompt, cfg):
        calls.append(prompt)
        raise llm_judge.LLMCallError(category)

    monkeypatch.setattr(llm_judge, "call_llm", fake_call)
    monkeypatch.setattr(llm_judge, "_sleep", lambda seconds: sleeps.append(seconds))
    result = llm_judge.judge_domain("a.com", "a", "online", cfg)
    assert result.verdict == "uncertain"
    assert result.fail_reason == category
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_http_status_category_mapping(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    class FakeResponse:
        def __init__(self, status):
            self.status_code = status

    monkeypatch.setattr(llm_judge, "get_api_key", lambda: "test-key")
    expected = {
        401: "auth_error",
        403: "auth_error",
        404: "model_or_endpoint_error",
        429: "rate_limit",
        500: "server_error",
        400: "http_error",
    }
    for status, category in expected.items():
        monkeypatch.setattr(
            llm_judge.requests, "post",
            lambda *args, _status=status, **kwargs: FakeResponse(_status),
        )
        with pytest.raises(llm_judge.LLMCallError) as exc_info:
            llm_judge.call_llm("prompt", cfg)
        assert exc_info.value.category == category


def test_judge_domain_cache_hit_skips_api(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    llm_judge.append_cache(_entry("a.com", "a", cfg, verdict="normal"), cfg["cache_path"])
    monkeypatch.setattr(
        llm_judge, "call_llm",
        lambda prompt, cfg: pytest.fail("cache hit must not call API"),
    )
    result = llm_judge.judge_domain("a.com", "a", "online", cfg)
    assert result.cache_hit
    assert result.verdict == "normal"


def test_budget_guard_all_when_under_limit():
    selected, rule = llm_judge.budget_guard(["a", "b", "c"], 800, 1)
    assert selected == ["a", "b", "c"]
    assert rule["applied"] is False


def test_budget_guard_stratified_is_deterministic():
    qnames = [f"q{i:02d}" for i in range(10)]
    strata = {q: ("A" if i < 5 else "B") for i, q in enumerate(qnames)}
    first, rule = llm_judge.budget_guard(qnames, 4, 7, strata)
    second, _ = llm_judge.budget_guard(qnames, 4, 7, strata)
    assert first == second
    assert len(first) == 4
    assert rule["applied"] is True
    assert rule["method"] == "stratified_by_source_algorithm"
    assert sum(item["selected"] for item in rule["strata"].values()) == 4


def test_run_judging_offline_summary(tmp_path, monkeypatch):
    features_path = tmp_path / "features.csv"
    _features([
        _row("q1.example", 1, 1, 0, 1, source="manual_dga", algorithm="word_digit"),
        _row("q2.example", 0, 1, 0, 1),
        _row("q3.example", 1, 0, 1, 1),
    ]).to_csv(features_path, index=False)

    def fake_judge(qname, registrable_label, mode, cfg):
        if qname == "q1.example":
            return llm_judge.VerdictResult("DGA", "随机", None, 0, False, "")
        return llm_judge.VerdictResult("uncertain", "", "no_cache", 0, False, "")

    monkeypatch.setattr(llm_judge, "judge_domain", fake_judge)
    summary = llm_judge.run_judging(features_path, "offline")
    assert summary["mode"] == "offline"
    assert summary["requested_unique"] == 2
    assert summary["verdict_counts"] == {"DGA": 1, "normal": 0, "uncertain": 1}
    assert summary["fail_reason_counts"] == {"no_cache": 1}
    assert summary["offline_coverage"] == 0.5
    assert summary["budget_applied"] is False
    assert summary["sampling_rule"] is None

"""改判逻辑测试（组长主笔，组员B 协助规则部分，D9）。

覆盖 docs/specs/project-roadmap.md 2.4 改判表：
- R=0 不请求 LLM；B=1 无论熵是否命中均保留告警
- E=1,B=0 且 verdict=normal 才改判；DGA/uncertain 保留告警
- API 失败/格式校验失败/缓存缺失 → 按 uncertain 保留告警并单列原因
- 缓存键变化后旧缓存失效
"""

import json

import pandas as pd
import pytest

from src import evaluate, llm_judge
from src.features import FEATURES_COLUMNS

ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
TEMPLATE = "判断 {{qname}} 的可注册标签 {{registrable_label}} 是否为 DGA。"


def _row(qname, label, e, b, r, split="test", client_ip="10.0.0.1",
         window_start="2026-11-01T00:00:00Z"):
    return {
        "client_ip": client_ip,
        "window_start": window_start,
        "qname": qname,
        "label": label,
        "source": "manual_dga" if label else "normal_list",
        "algorithm": "rand_alpha" if label else None,
        "split": split,
        "H": 3.0,
        "F": 2,
        "I": 5.0,
        "E": e,
        "B": b,
        "R": r,
        "llm_verdict": "",
        "llm_fail_reason": "",
        "hybrid": 0,
    }


def _features(rows):
    return pd.DataFrame(rows, columns=FEATURES_COLUMNS)


def _cfg(cache_path, max_retries=2, params=None):
    return {
        "model": "glm-4-flash",
        "endpoint": ENDPOINT,
        "template": TEMPLATE,
        "template_hash": "a" * 64,
        "temperature": 0,
        "max_tokens": 32,
        "params": params or {"temperature": 0, "max_tokens": 32},
        "cache_path": str(cache_path),
        "max_retries": max_retries,
    }


def _write_cache(path, qname, label, verdict, cfg):
    entry = llm_judge._cache_entry(
        qname, label, cfg["model"], llm_judge.sanitize_endpoint(cfg["endpoint"]),
        cfg["template_hash"], cfg["params"], 1, 200,
        json.dumps({"verdict": verdict, "reason": "测试"}),
        {"verdict": verdict, "reason": "测试"}, 1,
    )
    llm_judge.append_cache(entry, path)


def test_select_judgeable_only_e_and_not_b():
    df = _features([
        _row("q0.com", 0, 0, 0, 0),
        _row("q1.com", 0, 1, 0, 1),
        _row("q2.com", 1, 1, 1, 1),
        _row("q3.com", 0, 0, 1, 1),
        _row("q1.com", 0, 1, 0, 1, client_ip="10.0.0.2"),
    ])
    selected = llm_judge.select_judgeable(df)
    assert selected["qname"].tolist() == ["q1.com"]


def test_r0_and_b1_do_not_call_llm():
    df = _features([
        _row("q0.com", 0, 0, 0, 0),
        _row("q1.com", 0, 1, 1, 1),
    ])
    out = evaluate.apply_hybrid(df, {})
    assert out["llm_verdict"].tolist() == ["na", "na"]
    assert out["llm_fail_reason"].tolist() == ["", ""]
    assert out["hybrid"].tolist() == [0, 1]


def test_e1_b0_normal_is_withdrawn():
    df = _features([_row("q1.com", 0, 1, 0, 1)])
    out = evaluate.apply_hybrid(df, {
        "q1.com": {"verdict": "normal", "fail_reason": None},
    })
    assert int(out["hybrid"].iloc[0]) == 0
    assert out["llm_verdict"].iloc[0] == "normal"


@pytest.mark.parametrize("verdict", ["DGA", "uncertain"])
def test_e1_b0_dga_or_uncertain_is_retained(verdict):
    df = _features([_row("q1.com", 1, 1, 0, 1)])
    out = evaluate.apply_hybrid(df, {
        "q1.com": {"verdict": verdict, "fail_reason": None},
    })
    assert int(out["hybrid"].iloc[0]) == 1


def test_b1_normal_verdict_cannot_release():
    df = _features([_row("q1.com", 0, 1, 1, 1)])
    out = evaluate.apply_hybrid(df, {
        "q1.com": {"verdict": "normal", "fail_reason": None},
    })
    assert int(out["hybrid"].iloc[0]) == 1


def test_failed_verdict_is_uncertain_and_retained():
    df = _features([_row("q1.com", 1, 1, 0, 1)])
    out = evaluate.apply_hybrid(df, {
        "q1.com": {"verdict": "uncertain", "fail_reason": "timeout"},
    })
    assert int(out["hybrid"].iloc[0]) == 1
    assert out["llm_verdict"].iloc[0] == "uncertain"
    assert out["llm_fail_reason"].iloc[0] == "timeout"


def test_offline_without_cache_keeps_alert_and_no_network(tmp_path, monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("offline mode must not call the API")

    monkeypatch.setattr(llm_judge.requests, "post", _blocked)
    cfg = _cfg(tmp_path / "llm_cache.jsonl")
    result = llm_judge.judge_domain("q1.com", "q1", "offline", cfg)
    assert result.verdict == "uncertain"
    assert result.fail_reason == "no_cache"
    assert result.cache_hit is False


def test_offline_cache_hit_returns_verdict(tmp_path, monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("offline mode must not call the API")

    monkeypatch.setattr(llm_judge.requests, "post", _blocked)
    cache_path = tmp_path / "llm_cache.jsonl"
    cfg = _cfg(cache_path)
    _write_cache(cache_path, "q1.com", "q1", "normal", cfg)
    result = llm_judge.judge_domain("q1.com", "q1", "offline", cfg)
    assert result.verdict == "normal"
    assert result.fail_reason is None
    assert result.cache_hit is True


def test_cache_key_change_invalidates_old_entry(tmp_path):
    cache_path = tmp_path / "llm_cache.jsonl"
    cfg = _cfg(cache_path)
    _write_cache(cache_path, "q1.com", "q1", "normal", cfg)

    changed_hash = _cfg(cache_path)
    changed_hash["template_hash"] = "b" * 64
    result = llm_judge.judge_domain("q1.com", "q1", "offline", changed_hash)
    assert result.verdict == "uncertain"
    assert result.fail_reason == "no_cache"

    changed_params = _cfg(cache_path, params={"temperature": 0, "max_tokens": 64})
    result = llm_judge.judge_domain("q1.com", "q1", "offline", changed_params)
    assert result.verdict == "uncertain"
    assert result.fail_reason == "no_cache"


def test_format_error_returns_uncertain(tmp_path, monkeypatch):
    monkeypatch.setattr(
        llm_judge, "call_llm",
        lambda prompt, cfg: ("not json", 7),
    )
    cfg = _cfg(tmp_path / "llm_cache.jsonl")
    result = llm_judge.judge_domain("q1.com", "q1", "online", cfg)
    assert result.verdict == "uncertain"
    assert result.fail_reason == "format_error"
    assert not (tmp_path / "llm_cache.jsonl").exists()


def test_retry_exhausted_timeout_returns_uncertain(tmp_path, monkeypatch):
    attempts = []

    def _counting_timeout(prompt, cfg):
        attempts.append(1)
        raise llm_judge.LLMCallError("timeout")

    monkeypatch.setattr(llm_judge, "call_llm", _counting_timeout)
    monkeypatch.setattr(llm_judge, "_sleep", lambda seconds: None)
    cfg = _cfg(tmp_path / "llm_cache.jsonl", max_retries=2)
    result = llm_judge.judge_domain("q1.com", "q1", "online", cfg)
    assert result.verdict == "uncertain"
    assert result.fail_reason == "timeout"
    assert len(attempts) == 3


def test_parse_verdict_rejects_extra_fields():
    with pytest.raises(ValueError):
        llm_judge.parse_verdict(
            '{"verdict": "normal", "reason": "ok", "source": "normal_list"}'
        )

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


# 构造一行特征记录：label=1 时补 manual_dga/rand_alpha 元数据，H/F/I 取固定值
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


# 按 FEATURES_COLUMNS 契约列顺序把字典列表转成 DataFrame
def _features(rows):
    return pd.DataFrame(rows, columns=FEATURES_COLUMNS)


# 构造最小 LLM 配置：模型、端点、模板哈希、生成参数、缓存路径与重试次数
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


# 用官方缓存条目构造函数写入一条伪造缓存记录（attempt=1、HTTP 200）
def _write_cache(path, qname, label, verdict, cfg):
    entry = llm_judge._cache_entry(
        qname, label, cfg["model"], llm_judge.sanitize_endpoint(cfg["endpoint"]),
        cfg["template_hash"], cfg["params"], 1, 200,
        json.dumps({"verdict": verdict, "reason": "测试"}),
        {"verdict": verdict, "reason": "测试"}, 1,
    )
    llm_judge.append_cache(entry, path)


def test_select_judgeable_only_e_and_not_b():
    # 只有 E=1 且 B=0 的行进入 LLM 复核，重复 qname 去重后仅保留一条 q1.com
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
    # R=0 与 B=1 的行都不请求 LLM：结论记 na，hybrid 直接等于 R
    df = _features([
        _row("q0.com", 0, 0, 0, 0),
        _row("q1.com", 0, 1, 1, 1),
    ])
    out = evaluate.apply_hybrid(df, {})
    assert out["llm_verdict"].tolist() == ["na", "na"]
    assert out["llm_fail_reason"].tolist() == ["", ""]
    assert out["hybrid"].tolist() == [0, 1]


def test_e1_b0_normal_is_withdrawn():
    # E=1、B=0 且 verdict=normal 时撤销告警：hybrid 由 1 变为 0
    df = _features([_row("q1.com", 0, 1, 0, 1)])
    out = evaluate.apply_hybrid(df, {
        "q1.com": {"verdict": "normal", "fail_reason": None},
    })
    assert int(out["hybrid"].iloc[0]) == 0
    assert out["llm_verdict"].iloc[0] == "normal"


@pytest.mark.parametrize("verdict", ["DGA", "uncertain"])
def test_e1_b0_dga_or_uncertain_is_retained(verdict):
    # E=1、B=0 下 DGA 与 uncertain 都不撤销，告警保持 hybrid=1
    df = _features([_row("q1.com", 1, 1, 0, 1)])
    out = evaluate.apply_hybrid(df, {
        "q1.com": {"verdict": verdict, "fail_reason": None},
    })
    assert int(out["hybrid"].iloc[0]) == 1


def test_b1_normal_verdict_cannot_release():
    # B=1 时 LLM 判 normal 也不能撤销，告警保持 hybrid=1
    df = _features([_row("q1.com", 0, 1, 1, 1)])
    out = evaluate.apply_hybrid(df, {
        "q1.com": {"verdict": "normal", "fail_reason": None},
    })
    assert int(out["hybrid"].iloc[0]) == 1


def test_failed_verdict_is_uncertain_and_retained():
    # 调用失败按 uncertain 保留告警，并原样记录失败原因 timeout
    df = _features([_row("q1.com", 1, 1, 0, 1)])
    out = evaluate.apply_hybrid(df, {
        "q1.com": {"verdict": "uncertain", "fail_reason": "timeout"},
    })
    assert int(out["hybrid"].iloc[0]) == 1
    assert out["llm_verdict"].iloc[0] == "uncertain"
    assert out["llm_fail_reason"].iloc[0] == "timeout"


def test_offline_without_cache_keeps_alert_and_no_network(tmp_path, monkeypatch):
    # 离线且无缓存：返回 uncertain/no_cache、cache_hit=False，且 HTTP 被拦截证明未联网
    def _blocked(*args, **kwargs):
        raise AssertionError("offline mode must not call the API")

    monkeypatch.setattr(llm_judge.requests, "post", _blocked)
    cfg = _cfg(tmp_path / "llm_cache.jsonl")
    result = llm_judge.judge_domain("q1.com", "q1", "offline", cfg)
    assert result.verdict == "uncertain"
    assert result.fail_reason == "no_cache"
    assert result.cache_hit is False


def test_offline_cache_hit_returns_verdict(tmp_path, monkeypatch):
    # 离线命中缓存时直接返回缓存结论 normal，cache_hit=True，同样不联网
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
    # 模板哈希或生成参数变化都会改变缓存键，旧缓存失效后离线只能返回 uncertain/no_cache
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
    # 模型返回非 JSON 时记为 format_error/uncertain，且失败结果不写入缓存文件
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
    # 可重试错误耗尽重试上限（max_retries=2 即共 3 次调用）后返回 uncertain/timeout
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
    # 解析结果包含非白名单字段（如泄漏的 source 元数据）时抛 ValueError
    with pytest.raises(ValueError):
        llm_judge.parse_verdict(
            '{"verdict": "normal", "reason": "ok", "source": "normal_list"}'
        )

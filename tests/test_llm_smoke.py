import json

import pytest
import requests

from src import llm_judge


# 伪造 requests 响应对象，提供 status_code、json() 与 headers
class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body


# 写入临时 config.json，仅含冒烟测试所需的模型配置
def write_config(tmp_path, base_url="https://open.bigmodel.cn/api/paas/v4/chat/completions"):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model": {
        "name": "GLM-4-Flash",
        "base_url": base_url,
        "timeout_seconds": 3,
        "temperature": 0,
        "max_tokens": 32,
    }}), encoding="utf-8")
    return path


# 构造成功响应并记录调用参数（URL 与 kwargs），供断言请求内容
def fake_success_post(calls):
    def post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(body={
            "model": "glm-4-flash",
            "choices": [{"message": {"content": '{"status":"ok"}'}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }, headers=requests.structures.CaseInsensitiveDict({"request-id": "request-123"}))

    return post


# 返回固定 content 的响应，用于校验 JSON 内容与模型名检查
def fake_post_with_content(content, model="glm-4-flash"):
    def post(url, **kwargs):
        return FakeResponse(body={
            "model": model,
            "choices": [{"message": {"content": content}}],
        })

    return post


# 返回固定状态码的响应，body 带敏感哨兵串用于泄漏检查
def fake_post_with_status(status):
    def post(url, **kwargs):
        return FakeResponse(status_code=status, body={"error": "secret-sentinel"})

    return post


# 官方端点两种写法：默认端口与显式 :443 都应通过校验
@pytest.mark.parametrize(("base_url", "expected_endpoint"), [
    (
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    ),
    (
        "https://open.bigmodel.cn:443/api/paas/v4/chat/completions",
        "https://open.bigmodel.cn:443/api/paas/v4/chat/completions",
    ),
])
def test_smoke_posts_bearer_json_and_records_valid_response(
    tmp_path, monkeypatch, base_url, expected_endpoint
):
    # 请求带 Bearer 头与 JSON 体（json_object、temperature=0）；证据只记 10 个白名单字段，不含 Key 与原始响应
    calls = []
    monkeypatch.setattr(llm_judge, "get_api_key", lambda: "secret-sentinel")
    monkeypatch.setattr(requests, "post", fake_success_post(calls))
    evidence_path = tmp_path / "evidence.json"
    result = llm_judge.run_smoke_test(
        config_path=write_config(tmp_path, base_url), evidence_path=evidence_path
    )
    evidence_text = evidence_path.read_text(encoding="utf-8")
    evidence = json.loads(evidence_text)

    assert result["success"] is True
    assert calls[0]["url"] == expected_endpoint
    assert calls[0]["kwargs"]["headers"]["Authorization"] == "Bearer secret-sentinel"
    assert calls[0]["kwargs"]["allow_redirects"] is False
    assert calls[0]["kwargs"]["timeout"] == 3
    assert calls[0]["kwargs"]["json"]["model"] == "GLM-4-Flash"
    assert calls[0]["kwargs"]["json"]["temperature"] == 0
    assert calls[0]["kwargs"]["json"]["max_tokens"] == 32
    assert calls[0]["kwargs"]["json"]["response_format"] == {"type": "json_object"}
    assert "json" in calls[0]["kwargs"]["json"]["messages"][0]["content"].lower()
    assert evidence["success"] is True
    assert evidence["response_json_valid"] is True
    assert evidence["request_id"] == "request-123"
    assert evidence["usage"] == {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    assert set(evidence) == {
        "checked_at", "model", "endpoint", "http_status", "latency_ms",
        "request_id", "usage", "response_json_valid", "success", "error_category",
    }
    assert "secret-sentinel" not in evidence_text
    assert '{"status":"ok"}' not in evidence_text


# 各类非官方/畸形端点：伪装子域、IP、异常端口、错误路径、userinfo、尾随空白与控制字符
@pytest.mark.parametrize("base_url", [
    "https://example.com/api/paas/v4/chat/completions",
    "https://open.bigmodel.cn.attacker.example/api/paas/v4/chat/completions",
    "https://192.0.2.1/api/paas/v4/chat/completions",
    "https://open.bigmodel.cn:8443/api/paas/v4/chat/completions",
    "https://open.bigmodel.cn/api/paas/v4/other",
    "https://@open.bigmodel.cn/api/paas/v4/chat/completions",
    "https://open.bigmodel.cn/api/paas/v4/chat/completions?",
    "https://open.bigmodel.cn/api/paas/v4/chat/completions#",
    " https://open.bigmodel.cn/api/paas/v4/chat/completions",
    "https://open.bigmodel.cn/api/paas/v4/chat/completions ",
    "https://open.bigmodel.cn\t/api/paas/v4/chat/completions",
    "https://open.bigmodel.cn\r/api/paas/v4/chat/completions",
    "https://open.bigmodel.cn\n/api/paas/v4/chat/completions",
    "https://open.bigmodel.cn\x00/api/paas/v4/chat/completions",
    "https://open.bigmodel.cn\x01/api/paas/v4/chat/completions",
    "https://open.bigmodel.cn\x1f/api/paas/v4/chat/completions",
    "https://open.bigmodel.cn\x7f/api/paas/v4/chat/completions",
])
def test_smoke_rejects_non_official_endpoint_before_credentials_or_request(tmp_path, monkeypatch, base_url):
    # 非官方端点在取 Key 与发请求前即被拒，返回 config_error
    credential_calls = []
    request_calls = []

    def fake_api_key():
        credential_calls.append(True)
        return "test-only-key"

    def fake_post(url, **kwargs):
        request_calls.append(url)
        return FakeResponse(status_code=400)

    monkeypatch.setattr(llm_judge, "get_api_key", fake_api_key)
    monkeypatch.setattr(requests, "post", fake_post)
    result = llm_judge.run_smoke_test(
        config_path=write_config(tmp_path, base_url),
        evidence_path=tmp_path / "evidence.json",
    )

    assert (result["error_category"], credential_calls, request_calls) == ("config_error", [], [])


def test_smoke_does_not_send_request_without_api_key(tmp_path, monkeypatch):
    # 缺少 API Key 时返回 no_api_key，不发送请求且证据不含 Key
    def missing_key():
        raise RuntimeError("missing key secret-sentinel")

    monkeypatch.setattr(llm_judge, "get_api_key", missing_key)
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: pytest.fail("request must not be sent"),
    )
    evidence_path = tmp_path / "evidence.json"
    result = llm_judge.run_smoke_test(config_path=write_config(tmp_path), evidence_path=evidence_path)

    assert result["error_category"] == "no_api_key"
    assert "secret-sentinel" not in evidence_path.read_text(encoding="utf-8")


def test_smoke_rejects_invalid_json_content(tmp_path, monkeypatch):
    # 响应内容不是合法 JSON 时记 format_error
    monkeypatch.setattr(llm_judge, "get_api_key", lambda: "secret-sentinel")
    monkeypatch.setattr(requests, "post", fake_post_with_content("not-json"))
    result = llm_judge.run_smoke_test(config_path=write_config(tmp_path), evidence_path=tmp_path / "evidence.json")

    assert result["success"] is False
    assert result["error_category"] == "format_error"


def test_smoke_rejects_unexpected_model(tmp_path, monkeypatch):
    # 响应模型与配置不一致时记 format_error
    monkeypatch.setattr(llm_judge, "get_api_key", lambda: "secret-sentinel")
    monkeypatch.setattr(
        requests,
        "post",
        fake_post_with_content('{"status":"ok"}', model="other-model"),
    )
    result = llm_judge.run_smoke_test(config_path=write_config(tmp_path), evidence_path=tmp_path / "evidence.json")

    assert result["success"] is False
    assert result["error_category"] == "format_error"


def test_smoke_classifies_timeout_without_recording_exception_text(tmp_path, monkeypatch):
    # 超时归类为 timeout，证据文件不记录异常原文
    def timeout(url, **kwargs):
        raise requests.Timeout("secret-sentinel")

    monkeypatch.setattr(llm_judge, "get_api_key", lambda: "secret-sentinel")
    monkeypatch.setattr(requests, "post", timeout)
    evidence_path = tmp_path / "evidence.json"
    result = llm_judge.run_smoke_test(config_path=write_config(tmp_path), evidence_path=evidence_path)

    assert result["error_category"] == "timeout"
    assert "secret-sentinel" not in evidence_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(("status", "category"), [
    (401, "auth_error"),
    (403, "auth_error"),
    (404, "model_or_endpoint_error"),
    (429, "rate_limit"),
    (503, "server_error"),
    (400, "http_error"),
])
def test_smoke_classifies_http_failures_without_recording_secrets(tmp_path, monkeypatch, status, category):
    # 各 HTTP 状态码映射到对应错误类别，证据文件不含敏感串
    monkeypatch.setattr(llm_judge, "get_api_key", lambda: "secret-sentinel")
    monkeypatch.setattr(requests, "post", fake_post_with_status(status))
    evidence_path = tmp_path / "evidence.json"
    result = llm_judge.run_smoke_test(config_path=write_config(tmp_path), evidence_path=evidence_path)

    assert result["error_category"] == category
    assert "secret-sentinel" not in evidence_path.read_text(encoding="utf-8")


def test_module_cli_runs_smoke_subcommand(monkeypatch, capsys):
    # CLI --smoke 返回 0，且标准输出不含敏感信息
    monkeypatch.setattr(llm_judge, "run_smoke_test", lambda: {"success": True})

    assert llm_judge.main(["--smoke"]) == 0
    assert "secret-sentinel" not in capsys.readouterr().out


def test_module_cli_does_not_print_exception_details(monkeypatch, capsys):
    # CLI 失败返回 1，且不打印异常详情（防泄漏）
    monkeypatch.setattr(llm_judge, "run_smoke_test", lambda: {
        "success": False,
        "error_category": "auth_error",
        "exception": "secret-sentinel",
    })

    assert llm_judge.main(["--smoke"]) == 1
    assert "secret-sentinel" not in capsys.readouterr().out

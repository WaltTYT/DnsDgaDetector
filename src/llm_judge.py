"""GLM-4-Flash 语义复核：调用、校验、缓存与改判。

负责人：组长（D7–D9）
参考：docs/specs/project-roadmap.md 2.4 改判规则、2.5 调用预算与缓存

待实现要点：
- 只送 E=1 且 B=0 的样本；输入为规范化域名+注册域标签，不含真值/生成来源
- 输出校验：{"verdict": "DGA|normal|uncertain", "reason": "..."} 枚举与字段校验
- hybrid = B OR (E AND verdict != normal)；失败/超时/缓存缺失按 uncertain 处理并单列原因
- 串行调用、有上限指数退避最多 3 次；temperature=0 并记录生成参数
- 密钥解析：优先环境变量 ZHIPU_API_KEY，缺省自动回退根目录 api.txt（已被 .gitignore 忽略）
- 缓存键含输入+模型+模板哈希+参数；原始响应入 results/llm_cache.jsonl；Key 不得写入缓存
- 正式调用前统计可复核样本数与去重请求数（约 800 内），先测 20 个样本估时
- 离线模式绝不请求 API，报告缓存覆盖率
"""

import argparse
import json
import os
import re
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests


def get_api_key() -> str:
    """优先环境变量，其次根目录 api.txt；两者都没有时报错，不静默返回空。"""
    key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if not key:
        env_file = Path(__file__).resolve().parent.parent / "api.txt"
        if env_file.exists():
            key = env_file.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError("未找到 API Key：请设置环境变量 ZHIPU_API_KEY，或将密钥写入根目录 api.txt")
    return key


def _smoke_result(model: str | None = None, endpoint: str | None = None) -> dict:
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "model": model,
        "endpoint": endpoint,
        "http_status": None,
        "latency_ms": None,
        "request_id": None,
        "usage": None,
        "response_json_valid": None,
        "success": False,
        "error_category": None,
    }


def _write_smoke_result(path: Path, result: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _sanitize_endpoint(value: str) -> str:
    if re.search(r"[\s\x00-\x1f\x7f]", value):
        raise ValueError("invalid endpoint")
    parts = urlsplit(value)
    if (
        parts.scheme.lower() != "https"
        or parts.hostname is None
        or parts.hostname.lower() != "open.bigmodel.cn"
        or parts.netloc.lower() not in {"open.bigmodel.cn", "open.bigmodel.cn:443"}
        or parts.path != "/api/paas/v4/chat/completions"
        or "?" in value
        or "#" in value
    ):
        raise ValueError("invalid endpoint")
    if parts.port not in (None, 443):
        raise ValueError("invalid endpoint")
    authority = "open.bigmodel.cn:443" if parts.port == 443 else "open.bigmodel.cn"
    return urlunsplit(("https", authority, parts.path, "", ""))


def _safe_usage(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    usage = {
        name: value[name]
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance(value.get(name), int) and not isinstance(value[name], bool)
    }
    return usage or None


def _request_id(headers: object) -> str | None:
    if not isinstance(headers, Mapping):
        return None
    lowered = {str(name).lower(): str(value) for name, value in headers.items()}
    value = lowered.get("request-id") or lowered.get("x-request-id")
    if value and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        return value
    return None


def _http_error_category(status: int) -> str:
    if status in (401, 403):
        return "auth_error"
    if status == 404:
        return "model_or_endpoint_error"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server_error"
    return "http_error"


def run_smoke_test(config_path: str | Path | None = None, evidence_path: str | Path | None = None) -> dict:
    project_root = Path(__file__).resolve().parent.parent
    config_file = Path(config_path) if config_path is not None else project_root / "config.json"
    output_file = Path(evidence_path) if evidence_path is not None else project_root / "report" / "evidence" / "d1_glm_smoke.json"
    result = _smoke_result()

    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
        model_config = config["model"]
        model = model_config["name"]
        endpoint = model_config["base_url"]
        timeout = model_config["timeout_seconds"]
        temperature = model_config["temperature"]
        max_tokens = model_config["max_tokens"]
        if not isinstance(model, str) or not model.strip():
            raise ValueError("invalid model")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("invalid timeout")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ValueError("invalid temperature")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError("invalid max_tokens")
        endpoint = _sanitize_endpoint(endpoint)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
        result["error_category"] = "config_error"
        return _write_smoke_result(output_file, result)

    result["model"] = model
    result["endpoint"] = endpoint

    try:
        api_key = get_api_key()
    except RuntimeError:
        result["error_category"] = "no_api_key"
        return _write_smoke_result(output_file, result)
    except (OSError, UnicodeError):
        result["error_category"] = "credential_error"
        return _write_smoke_result(output_file, result)

    request_body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": 'Return exactly one JSON object and no Markdown: {"status":"ok"}',
        }],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    started = time.perf_counter()
    try:
        response = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=request_body,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.Timeout:
        result["error_category"] = "timeout"
        result["latency_ms"] = round((time.perf_counter() - started) * 1000)
        return _write_smoke_result(output_file, result)
    except requests.RequestException:
        result["error_category"] = "network_error"
        result["latency_ms"] = round((time.perf_counter() - started) * 1000)
        return _write_smoke_result(output_file, result)

    result["latency_ms"] = round((time.perf_counter() - started) * 1000)
    result["http_status"] = response.status_code
    result["request_id"] = _request_id(response.headers)
    if response.status_code < 200 or response.status_code >= 300:
        result["error_category"] = _http_error_category(response.status_code)
        return _write_smoke_result(output_file, result)

    try:
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
            raise ValueError("invalid response model")
        if payload["model"].casefold() != model.casefold():
            raise ValueError("unexpected response model")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("invalid choices")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ValueError("invalid message")
        content = json.loads(message["content"])
        result["response_json_valid"] = isinstance(content, dict) and content.get("status") == "ok"
        result["usage"] = _safe_usage(payload.get("usage"))
        if not result["response_json_valid"]:
            raise ValueError("invalid JSON result")
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        result["error_category"] = "format_error"
        return _write_smoke_result(output_file, result)

    result["success"] = True
    return _write_smoke_result(output_file, result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GLM connectivity smoke check")
    parser.add_argument("--smoke", action="store_true", help="send one JSON smoke request")
    args = parser.parse_args(argv)
    if not args.smoke:
        parser.print_help()
        return 2

    result = run_smoke_test()
    if result["success"]:
        print("GLM smoke succeeded")
        return 0
    print(f"GLM smoke failed: {result['error_category']}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

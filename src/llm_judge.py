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
import hashlib
import json
import os
import random
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import requests

try:
    from features import FEATURES_COLUMNS, extract_label
except ImportError:
    from .features import FEATURES_COLUMNS, extract_label


def get_api_key() -> str:
    """优先环境变量，其次根目录 api.txt；两者都没有时报错，不静默返回空。"""
    # 读取后统一去除首尾空白，空字符串视为未配置
    key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if not key:
        env_file = Path(__file__).resolve().parent.parent / "api.txt"
        if env_file.exists():
            key = env_file.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError("未找到 API Key：请设置环境变量 ZHIPU_API_KEY，或将密钥写入根目录 api.txt")
    return key


def _smoke_result(model: str | None = None, endpoint: str | None = None) -> dict:
    # 冒烟测试结果骨架：先全部置空或失败，后续按执行阶段逐项填充
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
    # 以 UTF-8 JSON 写入证据文件（自动建目录）并原样返回结果
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _sanitize_endpoint(value: str) -> str:
    # 端点白名单校验：仅允许 https 的 open.bigmodel.cn 指定路径，拒绝控制字符与查询串，
    # 通过后返回去掉 query/fragment 的规范 URL
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
    # 只保留整型 token 统计字段（排除 bool），没有任何有效字段时返回 None
    if not isinstance(value, dict):
        return None
    usage = {
        name: value[name]
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance(value.get(name), int) and not isinstance(value[name], bool)
    }
    return usage or None


def _request_id(headers: object) -> str | None:
    # 从响应头取 request-id 或 x-request-id，仅接受受限字符格式，不符则返回 None
    if not isinstance(headers, Mapping):
        return None
    lowered = {str(name).lower(): str(value) for name, value in headers.items()}
    value = lowered.get("request-id") or lowered.get("x-request-id")
    if value and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        return value
    return None


def _http_error_category(status: int) -> str:
    # 把 HTTP 状态码归类为鉴权、模型/端点、限流、服务端与其他错误
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
    # 联网冒烟测试：按“读配置→取 Key→发请求→校验响应”分阶段执行，任一阶段失败即落盘并返回
    project_root = Path(__file__).resolve().parent.parent
    config_file = Path(config_path) if config_path is not None else project_root / "config.json"
    output_file = Path(evidence_path) if evidence_path is not None else project_root / "report" / "evidence" / "d1_glm_smoke.json"
    result = _smoke_result()

    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
        model_config = config["model"]
        # 读取并逐项校验模型配置，任何一项不合法都归为 config_error
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
        # Key 未配置与 Key 文件读取失败分别记为不同类别，便于排查
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
    # 只发一次请求且不跟随重定向，避免把凭证带到其他主机
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
        # 校验响应模型名、choices 与 message 结构，并要求 content 解析后为 {"status":"ok"}
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


# 判定结果允许的枚举值
VERDICTS = ("DGA", "normal", "uncertain")
# 可重试的失败类别：仅这些会触发退避重试
RETRYABLE_REASONS = {"timeout", "rate_limit", "server_error", "network_error"}
# 指数退避等待秒数，按尝试次数依次取用，超出则复用最后一档
BACKOFF_SECONDS = (1, 2, 4)
# 缓存条目必须且只能包含的字段集合，用于校验缓存文件结构与完整性
CACHE_REQUIRED_KEYS = {
    "cache_key", "input", "model", "endpoint", "prompt_template_hash",
    "params", "attempt_count", "http_status", "raw_response", "parsed",
    "latency_ms", "created_at",
}


@dataclass(frozen=True)
class VerdictResult:
    # 单域名判定结果：fail_reason 非空表示无缓存或调用失败，cache_hit 标记是否命中缓存
    verdict: str
    reason: str = ""
    fail_reason: str | None = None
    latency_ms: int = 0
    cache_hit: bool = False
    raw_response: str = ""


class LLMCallError(Exception):
    # 调用异常，category 携带错误类别，供上层判断是否重试
    def __init__(self, category: str, message: str = "") -> None:
        super().__init__(message or category)
        self.category = category


def _clock() -> float:
    # 统一计时入口
    return time.perf_counter()


def _sleep(seconds: float) -> None:
    # 统一休眠入口，供重试退避使用
    time.sleep(seconds)


def load_template(path: str | Path) -> str:
    # 读取模板文件，只返回分隔行 --- 之后的正文，分隔行前的内容不计入模板
    text = Path(path).read_text(encoding="utf-8")
    match = re.search(r"(?m)^---\s*$", text)
    if not match:
        raise ValueError("template separator line missing")
    body = text[match.end():].lstrip("\n")
    if not body:
        raise ValueError("template body empty")
    return body


def template_hash(path: str | Path) -> str:
    # 对模板正文取 SHA-256，模板一改哈希即变，从而使旧缓存自动失效
    body = load_template(path)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _validate_features(df: pd.DataFrame) -> None:
    # 要求列名与顺序严格等于特征契约，不满足即报错
    actual = list(df.columns)
    if actual != FEATURES_COLUMNS:
        raise ValueError(
            f"features columns mismatch.\n"
            f"  expected: {FEATURES_COLUMNS}\n"
            f"  actual:   {actual}"
        )


def select_judgeable(features_df: pd.DataFrame) -> pd.DataFrame:
    # 只挑可复核样本：E=1 且 B=0；同一 qname 去重保留首条，保证每个域名只请求一次
    _validate_features(features_df)
    selected = features_df[
        (features_df["E"] == 1) & (features_df["B"] == 0)
    ].copy()
    selected = selected.drop_duplicates(subset=["qname"], keep="first")
    return selected.reset_index(drop=True)


def build_prompt(qname: str, registrable_label: str, template: str) -> str:
    # 渲染提示模板：校验入参无换行与占位符注入，拒绝未知变量、残留占位符和元数据泄漏
    if not isinstance(qname, str) or not qname:
        raise ValueError("qname must be a non-empty string")
    if not isinstance(registrable_label, str):
        raise ValueError("registrable_label must be a string")
    for value in (qname, registrable_label):
        if "\n" in value or "\r" in value or "{{" in value or "}}" in value:
            raise ValueError("invalid render value")
    unknown = set(re.findall(r"\{\{([^{}]+)\}\}", template)) - {
        # 模板只允许这两个变量，其他占位符一律视为错误
        "qname", "registrable_label"
    }
    if unknown:
        raise ValueError(f"unknown template variables: {sorted(unknown)}")
    prompt = template.replace("{{qname}}", qname).replace(
        "{{registrable_label}}", registrable_label
    )
    if "{{" in prompt or "}}" in prompt:
        raise ValueError("unrendered template placeholder remains")
    for token in ("source=", "algorithm=", "split="):
        # 元数据字段不得进入提示词，否则构成标签泄漏
        if token in prompt:
            raise ValueError(f"metadata token leaked into prompt: {token}")
    return prompt


def cache_key(qname: str, registrable_label: str, model: str,
              template_hash_value: str, params: dict, endpoint: str) -> str:
    # 缓存键：把输入、模型、端点、模板哈希与生成参数序列化为规范 JSON 后取 SHA-256
    payload = {
        "input": {"qname": qname, "registrable_label": registrable_label},
        "model": model,
        "endpoint": endpoint,
        "prompt_template_hash": template_hash_value,
        "params": params,
    }
    # sort_keys 与紧凑分隔符保证同样输入得到同一串字节
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_verdict(raw_text: str) -> tuple[str, str]:
    # 解析模型输出：先剥离 Markdown 代码围栏，再校验恰好包含 verdict/reason 两字段；
    # verdict 必须在枚举内，reason 去空白后截断到 120 字符
    if not isinstance(raw_text, str):
        raise ValueError("raw verdict must be a string")
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text, count=1)
        text = re.sub(r"\s*```$", "", text, count=1).strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("verdict payload must be an object")
    if set(payload) != {"verdict", "reason"}:
        raise ValueError("verdict payload must contain exactly verdict and reason")
    verdict = payload["verdict"]
    reason = payload["reason"]
    if verdict not in VERDICTS:
        raise ValueError(f"invalid verdict: {verdict}")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be a non-empty string")
    return verdict, reason.strip()[:120]


def generation_params(model_cfg: dict) -> dict:
    # 提取参与缓存键的生成参数，仅保留配置中实际存在的项
    return {
        name: model_cfg[name]
        for name in ("temperature", "max_tokens")
        if name in model_cfg
    }


def sanitize_endpoint(value: str) -> str:
    # 公开包装，供评估等模块复用同一套端点校验
    return _sanitize_endpoint(value)


def _call_llm_once(prompt: str, cfg: dict) -> tuple[str, int, int]:
    # 执行一次请求：校验配置与 Key，发送 POST 后校验响应结构与模型名；
    # 任一步失败都抛 LLMCallError 并携带错误类别，返回 (内容, 耗时毫秒, HTTP 状态码)
    try:
        model = cfg["model"]
        endpoint = _sanitize_endpoint(cfg["endpoint"])
        timeout = cfg["timeout"]
        temperature = cfg["temperature"]
        max_tokens = cfg["max_tokens"]
        if not isinstance(model, str) or not model.strip():
            raise ValueError("invalid model")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("invalid timeout")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ValueError("invalid temperature")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError("invalid max_tokens")
    except (KeyError, TypeError, ValueError, AttributeError):
        # 配置缺失或取值不合法统一归为 config_error，不做重试
        raise LLMCallError("config_error")

    try:
        api_key = get_api_key()
    except RuntimeError:
        raise LLMCallError("no_api_key")
    except (OSError, UnicodeError):
        raise LLMCallError("credential_error")

    request_body = {
        # 请求体固定 temperature/max_tokens 并要求返回 JSON 对象
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
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
        raise LLMCallError("timeout")
    except requests.RequestException:
        raise LLMCallError("network_error")
    latency_ms = round((time.perf_counter() - started) * 1000)
    if response.status_code < 200 or response.status_code >= 300:
        raise LLMCallError(_http_error_category(response.status_code))
    try:
        payload = response.json()
        # 响应必须为对象、携带同名模型、choices 首项含字符串 content，否则视为格式错误
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
        content = message["content"]
        # 空内容视为格式错误，避免把空串当有效回答缓存
        if not content.strip():
            raise ValueError("empty content")
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise LLMCallError("format_error")
    return content, latency_ms, response.status_code


def call_llm(prompt: str, cfg: dict) -> tuple[str, int]:
    # 对外封装：只返回响应内容与耗时，丢弃状态码
    content, latency_ms, _ = _call_llm_once(prompt, cfg)
    return content, latency_ms


def load_cache(path: str | Path) -> dict[str, dict]:
    # 逐行读取 JSONL 缓存并校验字段集合、input/parsed 结构与缓存键唯一性，返回 cache_key 到条目映射
    cache_path = Path(path)
    if not cache_path.exists():
        return {}
    entries: dict[str, dict] = {}
    with cache_path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            text = line.strip()
            # 跳过空行，其余每行必须是一个完整条目对象
            if not text:
                continue
            entry = json.loads(text)
            if not isinstance(entry, dict):
                raise ValueError(f"cache entry line {line_no} must be an object")
            missing = CACHE_REQUIRED_KEYS - set(entry)
            extra = set(entry) - CACHE_REQUIRED_KEYS
            if missing or extra:
                raise ValueError(
                    f"cache entry line {line_no} fields mismatch: "
                    f"missing={sorted(missing)} extra={sorted(extra)}"
                )
            input_obj = entry["input"]
            if not isinstance(input_obj, dict) or set(input_obj) != {"qname", "registrable_label"}:
                raise ValueError(f"cache entry line {line_no} input fields mismatch")
            if not isinstance(entry["params"], dict):
                raise ValueError(f"cache entry line {line_no} params must be an object")
            parsed = entry["parsed"]
            if not isinstance(parsed, dict) or set(parsed) != {"verdict", "reason"}:
                raise ValueError(f"cache entry line {line_no} parsed fields mismatch")
            key = entry["cache_key"]
            # 缓存键重复说明文件被写脏，直接报错而不是静默覆盖
            if key in entries:
                raise ValueError(f"duplicate cache_key: {key}")
            entries[key] = entry
    return entries


def append_cache(entry: dict, path: str | Path) -> None:
    # 追加一条缓存：先校验字段集合，已存在相同缓存键则跳过，否则以单行 JSON 追加
    missing = CACHE_REQUIRED_KEYS - set(entry)
    extra = set(entry) - CACHE_REQUIRED_KEYS
    if missing or extra:
        raise ValueError(
            f"cache entry fields mismatch: missing={sorted(missing)} extra={sorted(extra)}"
        )
    cache_path = Path(path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    # 幂等写入：同一缓存键只保留首次结果
    if entry["cache_key"] in load_cache(cache_path):
        return
    with cache_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _cache_entry(qname: str, registrable_label: str, model: str, endpoint: str,
                 template_hash_value: str, params: dict, attempt_count: int,
                 http_status: int, raw_response: str,
                 parsed: dict, latency_ms: int) -> dict:
    # 组装一条缓存记录：重算缓存键，并记录生成参数、尝试次数、原始响应、解析结果与时间戳
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return {
        "cache_key": cache_key(
            qname, registrable_label, model, template_hash_value, params, endpoint
        ),
        "input": {"qname": qname, "registrable_label": registrable_label},
        "model": model,
        "endpoint": endpoint,
        "prompt_template_hash": template_hash_value,
        "params": params,
        "attempt_count": attempt_count,
        "http_status": http_status,
        "raw_response": raw_response,
        "parsed": parsed,
        "latency_ms": latency_ms,
        "created_at": created_at,
    }


def judge_domain(qname: str, registrable_label: str, mode: str, cfg: dict) -> VerdictResult:
    # 单域名改判：校验配置后先查缓存；离线模式缺缓存即告警，在线模式按上限指数退避重试
    if mode not in {"online", "offline"}:
        raise ValueError("mode must be online or offline")
    if not isinstance(cfg, dict):
        raise ValueError("cfg must be a dict")
    try:
        model = cfg["model"]
        endpoint = _sanitize_endpoint(cfg["endpoint"])
        template = cfg["template"]
        template_hash_value = cfg["template_hash"]
        params = cfg.get("params") or generation_params(cfg)
        cache_path = Path(cfg["cache_path"])
        max_retries = cfg.get("max_retries", 3)
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("invalid max_retries")
    except (KeyError, TypeError, ValueError, AttributeError):
        # 配置不合法时不发请求，直接按 uncertain 保留告警
        return VerdictResult("uncertain", "", "config_error", 0, False, "")

    key = cache_key(qname, registrable_label, model, template_hash_value, params, endpoint)
    cache = load_cache(cache_path)
    # 命中缓存直接返回，cache_hit 标记为 True 且不计耗时
    if key in cache:
        parsed = cache[key]["parsed"]
        return VerdictResult(
            parsed["verdict"], parsed.get("reason", ""), None, 0, True,
            cache[key].get("raw_response", ""),
        )
    if mode == "offline":
        # 离线模式绝不请求 API，缺缓存即记 no_cache
        return VerdictResult("uncertain", "", "no_cache", 0, False, "")

    prompt = build_prompt(qname, registrable_label, template)
    started = _clock()
    attempt_count = 0
    last_raw = ""
    for attempt in range(max_retries + 1):
        attempt_count = attempt + 1
        try:
            # 每轮重新调用一次接口；调用异常按类别决定是否退避重试
            content, _ = call_llm(prompt, cfg)
            http_status = 200
            last_raw = content
            try:
                verdict, reason = parse_verdict(content)
            except ValueError:
                # 输出 JSON 校验失败不重试，直接按 uncertain 保留原始响应
                latency_ms = round((_clock() - started) * 1000)
                return VerdictResult("uncertain", "", "format_error", latency_ms, False, last_raw)
            latency_ms = round((_clock() - started) * 1000)
            entry = _cache_entry(
                qname, registrable_label, model, endpoint, template_hash_value,
                params, attempt_count, http_status, content,
                {"verdict": verdict, "reason": reason}, latency_ms,
            )
            try:
                append_cache(entry, cache_path)
            except OSError:
                # 缓存写盘失败不影响本次判定结果
                pass
            return VerdictResult(verdict, reason, None, latency_ms, False, content)
        except LLMCallError as exc:
            # 仅可重试类别且仍有剩余次数时按退避表等待后重试，否则直接保留告警
            if exc.category in RETRYABLE_REASONS and attempt < max_retries:
                _sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
                continue
            latency_ms = round((_clock() - started) * 1000)
            return VerdictResult("uncertain", "", exc.category, latency_ms, False, last_raw)
    # 循环内各分支都会返回，此处仅作兜底
    return VerdictResult("uncertain", "", "server_error", 0, False, last_raw)


def budget_guard(qnames: list[str], max_unique: int, seed: int,
                 strata: dict[str, str] | None = None) -> tuple[list[str], dict]:
    # 请求预算控制：去重后未超上限则全取；超限时按分层或确定性抽样缩小规模，
    # 返回选中域名与抽样规则描述（供运行清单记录）
    unique = sorted(set(qnames))
    total = len(unique)
    if isinstance(max_unique, bool) or not isinstance(max_unique, int) or max_unique <= 0:
        raise ValueError("max_unique must be a positive integer")
    if total <= max_unique:
        # 未超预算：全部保留，不触发抽样
        return unique, {
            "applied": False,
            "max_unique": max_unique,
            "seed": seed,
            "total_unique": total,
            "selected": total,
            "method": "all",
        }

    rng = random.Random(seed)
    if not strata:
        # 无分层信息时用固定 seed 做确定性随机抽样，保证可复现
        selected = sorted(rng.sample(unique, max_unique))
        return selected, {
            "applied": True,
            "max_unique": max_unique,
            "seed": seed,
            "total_unique": total,
            "selected": max_unique,
            "method": "deterministic_sample",
            "strata": {},
        }

    groups: dict[str, list[str]] = {}
    # 按分层键分组，缺失分层的域名归入 unknown
    for qname in unique:
        groups.setdefault(strata.get(qname, "unknown"), []).append(qname)
    for name in groups:
        groups[name].sort()

    allocations: dict[str, int] = {}
    remainders = []
    # 按各层占比分配名额并向下取整，余数留待后续按最大余数补齐
    for name in sorted(groups):
        group_size = len(groups[name])
        exact = max_unique * group_size / total
        allocations[name] = min(int(exact), group_size)
        remainders.append((exact - int(exact), name))

    remaining = max_unique - sum(allocations.values())
    # 第一轮按余数从大到小补名额，余数相同按层名排序以保证确定性
    for _, name in sorted(remainders, key=lambda item: (-item[0], item[1])):
        if remaining <= 0:
            break
        if allocations[name] < len(groups[name]):
            allocations[name] += 1
            remaining -= 1
    for name in sorted(groups):
        # 第二轮把仍未分配完的名额按层序补入有余量的组
        if remaining <= 0:
            break
        spare = len(groups[name]) - allocations[name]
        add = min(spare, remaining)
        allocations[name] += add
        remaining -= add

    selected_qnames: list[str] = []
    for name in sorted(groups):
        # 在每层内用同一随机源抽取分配到的数量，最后统一排序
        if allocations[name]:
            selected_qnames.extend(rng.sample(groups[name], allocations[name]))
    selected_qnames.sort()
    return selected_qnames, {
        "applied": True,
        "max_unique": max_unique,
        "seed": seed,
        "total_unique": total,
        "selected": len(selected_qnames),
        "method": "stratified_by_source_algorithm",
        "strata": {
            name: {"available": len(groups[name]), "selected": allocations[name]}
            for name in sorted(groups)
        },
    }


def run_judging(features_path: str | Path, mode: str,
                config_path: str | Path | None = None) -> dict:
    # 批量复核流程：读配置与特征表，筛出可复核样本并按预算抽样，逐域名判定后汇总统计
    project_root = Path(__file__).resolve().parent.parent
    config_file = Path(config_path) if config_path is not None else project_root / "config.json"
    config = json.loads(config_file.read_text(encoding="utf-8"))
    model_cfg = config["model"]

    features_file = Path(features_path)
    features_df = pd.read_csv(features_file, dtype={"client_ip": str, "qname": str})
    _validate_features(features_df)
    selected = select_judgeable(features_df)

    strata = {
        # 分层键：有 algorithm 时用 source|algorithm，否则退化为 source
        row.qname: (
            f"{row.source}|{row.algorithm}"
            if pd.notna(row.algorithm) and str(row.algorithm)
            else str(row.source)
        )
        for row in selected.itertuples(index=False)
    }
    max_unique = int(config.get("budget", {}).get("max_unique_requests", 800))
    # 超预算时按固定 seed 抽样，并把最终选中的样本作为实际复核范围
    chosen, rule = budget_guard(
        selected["qname"].tolist(), max_unique, int(config.get("seed", 0)), strata
    )
    selected = selected[selected["qname"].isin(chosen)].copy()

    template_path = project_root / "prompts" / "dga_judge_prompt.md"
    # 模板正文与其哈希同时载入，哈希会进入缓存键
    template = load_template(template_path)
    template_hash_value = template_hash(template_path)
    cfg = {
        "model": model_cfg["name"],
        "endpoint": model_cfg["base_url"],
        "timeout": model_cfg["timeout_seconds"],
        "max_retries": model_cfg.get("max_retries", 3),
        "temperature": model_cfg["temperature"],
        "max_tokens": model_cfg["max_tokens"],
        "template": template,
        "template_hash": template_hash_value,
        "cache_path": project_root / "results" / "llm_cache.jsonl",
    }

    results = []
    # 串行逐个域名复核，注册域标签取自域名解析结果
    for qname in selected["qname"]:
        registrable_label = extract_label(qname)[0]
        results.append(judge_domain(qname, registrable_label, mode, cfg))

    requested = len(results)
    # 命中缓存数、各判定值计数与失败原因计数用于评估缓存覆盖率与调用健康度
    cache_hits = sum(1 for result in results if result.cache_hit)
    verdict_counts = {verdict: 0 for verdict in VERDICTS}
    fail_reason_counts: dict[str, int] = {}
    for result in results:
        verdict_counts[result.verdict] += 1
        if result.fail_reason:
            fail_reason_counts[result.fail_reason] = (
                fail_reason_counts.get(result.fail_reason, 0) + 1
            )
    covered = sum(1 for result in results if result.fail_reason is None)
    # 无失败原因的样本视为被有效覆盖，用于报告离线覆盖率
    return {
        "mode": mode,
        "requested_unique": requested,
        "cache_hits": cache_hits,
        "verdict_counts": verdict_counts,
        "fail_reason_counts": fail_reason_counts,
        "offline_coverage": round(covered / requested, 4) if requested else None,
        "budget_applied": rule["applied"],
        "sampling_rule": rule if rule["applied"] else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GLM semantic DGA judge")
    parser.add_argument("--smoke", action="store_true", help="send one JSON smoke request")
    parser.add_argument("--online", action="store_true", help="enable API calls")
    parser.add_argument("--offline", action="store_true", help="cache-only mode (default)")
    parser.add_argument(
        "--features", default=str(Path(__file__).resolve().parent.parent / "results" / "features.csv")
    )
    args = parser.parse_args(argv)
    if args.smoke:
        # 冒烟模式只发一次最小请求，成功与否以退出码体现
        result = run_smoke_test()
        if result["success"]:
            print("GLM smoke succeeded")
            return 0
        print(f"GLM smoke failed: {result['error_category']}")
        return 1

    # 默认离线，只有显式指定 --online 才允许发请求
    mode = "online" if args.online else "offline"
    try:
        summary = run_judging(args.features, mode)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # 预期异常只输出错误类型名并返回非零码，不打印堆栈
        print(json.dumps({"error": type(exc).__name__}, ensure_ascii=False))
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

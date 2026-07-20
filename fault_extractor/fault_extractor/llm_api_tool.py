from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_CONFIG_FILENAME = "llm_api_config.json"


def load_llm_config(config_path: Optional[str | Path] = None) -> Dict[str, Any]:
    """Load the JSON config file from the project root.

    By default, the config is expected to sit beside this file:
    ./llm_api_config.json
    """
    path = Path(config_path) if config_path else Path(__file__).resolve().parent / DEFAULT_CONFIG_FILENAME
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"LLM config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def call_llm(request_data: Dict[str, Any], config_path: Optional[str | Path] = None) -> Dict[str, Any]:
    """Call an OpenAI/BigModel-style chat completions API.

    Input:  Python dict.
        Common forms:
        1) {"prompt": "hello"}
        2) {"system_prompt": "...", "prompt": "hello"}
        3) {"messages": [{"role": "user", "content": "hello"}]}
        4) {"payload": {...}}  # advanced: directly provide the HTTP JSON payload

        Optional request-level overrides include:
        model, temperature, top_p, max_tokens, stream, do_sample, stop,
        presence_penalty, frequency_penalty, response_format, thinking,
        reasoning_effort, extra_payload, api_token, return_request_payload,
        timeout_sec, max_retries, retry_interval_sec, retry_status_codes.

    Output: Python dict.
        On success:
        {
          "success": True,
          "content": "assistant text",
          "raw_response": {...},
          "usage": {...} or None,
          "finish_reason": "stop" or None,
          "model": "...",
          "status_code": 200,
          "attempts": 1,
          "elapsed_sec": 0.123
        }

        On failure:
        {
          "success": False,
          "error_type": "...",
          "error": "...",
          "status_code": 401 or None,
          "raw_response": {...} or "..." or None,
          "attempts": 1,
          "elapsed_sec": 0.123
        }
    """
    start_time = time.time()

    if not isinstance(request_data, dict):
        return _error_result(
            error_type="InvalidInput",
            error="request_data must be a Python dict.",
            start_time=start_time,
        )

    try:
        config = load_llm_config(config_path)
        api_cfg = config.get("api", {})
        network_cfg = config.get("network", {})
        response_cfg = config.get("response", {})

        api_url = str(request_data.get("api_url") or api_cfg.get("url") or "").strip()
        if not api_url:
            return _error_result("ConfigError", "Missing API url in config['api']['url'].", start_time)

        token = _resolve_token(request_data, api_cfg)
        require_token = bool(api_cfg.get("require_token", True))
        if require_token and not token:
            env_names = ", ".join(api_cfg.get("token_env_vars", []))
            return _error_result(
                "ConfigError",
                "Missing API token. Put it in llm_api_config.json under api.token "
                f"or set one of these environment variables: {env_names}",
                start_time,
            )

        payload = _build_payload(request_data, config)
        headers = _build_headers(api_cfg, token)

        timeout_sec = float(request_data.get("timeout_sec", network_cfg.get("timeout_sec", 120)))
        max_retries = int(request_data.get("max_retries", network_cfg.get("max_retries", 3)))
        retry_interval_sec = float(
            request_data.get("retry_interval_sec", network_cfg.get("retry_interval_sec", 1.5))
        )
        retry_status_codes = set(
            int(x)
            for x in request_data.get(
                "retry_status_codes",
                network_cfg.get("retry_status_codes", [429, 500, 502, 503, 504]),
            )
        )

        last_error: Optional[Tuple[str, str, Optional[int], Any]] = None
        attempts = 0

        for attempt in range(max_retries + 1):
            attempts = attempt + 1
            try:
                status_code, raw_text = _post_json(api_url, headers, payload, timeout_sec)
                raw_response = _parse_json_or_text(raw_text)

                if not isinstance(raw_response, dict):
                    return _error_result(
                        "ResponseError",
                        "API returned a non-JSON-object response.",
                        start_time,
                        status_code=status_code,
                        raw_response=raw_response,
                        attempts=attempts,
                        request_payload=payload if request_data.get("return_request_payload") else None,
                    )

                content = _get_by_path(raw_response, response_cfg.get("content_path", ["choices", 0, "message", "content"]))
                message = _get_by_path(raw_response, ["choices", 0, "message"])
                content = _normalize_assistant_content(content, message)
                usage = _get_by_path(raw_response, response_cfg.get("usage_path", ["usage"]))
                finish_reason = _get_by_path(raw_response, response_cfg.get("finish_reason_path", ["choices", 0, "finish_reason"]))

                result = {
                    "success": True,
                    "content": content if content is not None else "",
                    "raw_response": raw_response,
                    "usage": usage,
                    "finish_reason": finish_reason,
                    "model": raw_response.get("model", payload.get("model")),
                    "status_code": status_code,
                    "attempts": attempts,
                    "elapsed_sec": round(time.time() - start_time, 3),
                }
                if request_data.get("return_request_payload"):
                    result["request_payload"] = payload
                return result

            except urllib.error.HTTPError as exc:
                raw_body = _read_error_body(exc)
                raw_response = _parse_json_or_text(raw_body)
                last_error = ("HTTPError", f"HTTP {exc.code}: {exc.reason}", exc.code, raw_response)
                should_retry = exc.code in retry_status_codes and attempt < max_retries
                if should_retry:
                    time.sleep(retry_interval_sec)
                    continue
                break
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = (type(exc).__name__, str(exc), None, None)
                if attempt < max_retries:
                    time.sleep(retry_interval_sec)
                    continue
                break
            except Exception as exc:  # Keep the tool dict-out even on unexpected failures.
                return _error_result(type(exc).__name__, str(exc), start_time, attempts=attempts)

        error_type, error, status_code, raw_response = last_error or ("UnknownError", "Unknown request failure.", None, None)
        return _error_result(
            error_type=error_type,
            error=error,
            start_time=start_time,
            status_code=status_code,
            raw_response=raw_response,
            attempts=attempts,
            request_payload=payload if request_data.get("return_request_payload") else None,
        )

    except Exception as exc:
        return _error_result(type(exc).__name__, str(exc), start_time)


def _build_payload(request_data: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    defaults = dict(config.get("request_defaults", {}))

    # Advanced mode: caller provides the API payload directly.
    if isinstance(request_data.get("payload"), dict):
        payload = defaults
        payload.update(request_data["payload"])
    else:
        payload = defaults
        messages = request_data.get("messages")
        if messages is None:
            messages = _messages_from_prompt(request_data)
        payload["messages"] = messages

        override_keys = [
            "model",
            "temperature",
            "top_p",
            "max_tokens",
            "stream",
            "do_sample",
            "stop",
            "presence_penalty",
            "frequency_penalty",
            "response_format",
            "thinking",
            "reasoning_effort",
            "tool_choice",
            "tools",
        ]
        for key in override_keys:
            if key in request_data:
                payload[key] = request_data[key]

    extra_payload = request_data.get("extra_payload")
    if isinstance(extra_payload, dict):
        payload.update(extra_payload)

    return payload


def _messages_from_prompt(request_data: Dict[str, Any]) -> List[Dict[str, str]]:
    prompt = request_data.get("prompt", request_data.get("user_prompt", ""))
    system_prompt = request_data.get("system_prompt", "")

    messages: List[Dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": str(system_prompt)})
    messages.append({"role": "user", "content": str(prompt)})
    return messages


def _resolve_token(request_data: Dict[str, Any], api_cfg: Dict[str, Any]) -> str:
    direct = str(request_data.get("api_token") or "").strip()
    if direct:
        return direct

    configured = str(api_cfg.get("token") or "").strip()
    if configured:
        return configured

    for env_name in api_cfg.get("token_env_vars", []):
        token = os.environ.get(str(env_name), "").strip()
        if token:
            return token
    return ""


def _build_headers(api_cfg: Dict[str, Any], token: str) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}

    extra_headers = api_cfg.get("extra_headers", {})
    if isinstance(extra_headers, dict):
        headers.update({str(k): str(v) for k, v in extra_headers.items()})

    if token:
        header_name = str(api_cfg.get("authorization_header", "Authorization") or "Authorization")
        scheme = str(api_cfg.get("authorization_scheme", "Bearer") or "").strip()
        headers[header_name] = f"{scheme} {token}".strip()

    return headers


def _post_json(api_url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout_sec: float) -> Tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(api_url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return int(resp.status), raw



def _normalize_assistant_content(content: Any, message: Any) -> Any:
    """Normalize common OpenAI-compatible response shapes.

    Most providers return a string in ``message.content``. Some return content
    parts, a JSON object, or function-call arguments while leaving content
    empty. Keeping this compatibility logic in the lightweight API wrapper
    makes structured-output callers more reliable without changing plain-text
    callers.
    """
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        text_parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict):
                value = part.get("text", part.get("content", part.get("value")))
                if value is not None:
                    text_parts.append(str(value))
        if text_parts:
            return "".join(text_parts)

    if isinstance(message, dict):
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if isinstance(function, dict):
                    arguments = function.get("arguments")
                    if arguments not in (None, ""):
                        return arguments

        function_call = message.get("function_call")
        if isinstance(function_call, dict) and function_call.get("arguments") not in (None, ""):
            return function_call["arguments"]

    return "" if content is None else content

def _parse_json_or_text(raw_text: str) -> Any:
    try:
        return json.loads(raw_text)
    except Exception:
        return raw_text


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _get_by_path(data: Any, path: Iterable[Any]) -> Any:
    current = data
    for part in path:
        try:
            if isinstance(current, dict):
                current = current[part]
            elif isinstance(current, list) and isinstance(part, int):
                current = current[part]
            else:
                return None
        except (KeyError, IndexError, TypeError):
            return None
    return current


def _error_result(
    error_type: str,
    error: str,
    start_time: float,
    status_code: Optional[int] = None,
    raw_response: Any = None,
    attempts: int = 0,
    request_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "success": False,
        "error_type": error_type,
        "error": error,
        "status_code": status_code,
        "raw_response": raw_response,
        "attempts": attempts,
        "elapsed_sec": round(time.time() - start_time, 3),
    }
    if request_payload is not None:
        result["request_payload"] = request_payload
    return result

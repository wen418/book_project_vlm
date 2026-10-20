#!/usr/bin/env python3
"""Small dependency-free client for a remote OpenAI-compatible VLM API."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


class VLMRequestError(RuntimeError):
    pass


class VLMOutputError(VLMRequestError):
    """The API returned an incomplete or empty model answer."""


RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


class OpenAICompatibleVLM:
    def __init__(
        self,
        api_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 30.0,
        max_tokens: int = 300,
        temperature: float = 0.0,
        use_json_object: bool = True,
        max_retries: int = 1,
        enable_thinking: Optional[bool] = None,
    ) -> None:
        self.api_url = api_url
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.use_json_object = use_json_object
        self.max_retries = max(0, int(max_retries))
        self.enable_thinking = enable_thinking

    def _require_credentials(self) -> None:
        hostname = (urlparse(self.api_url).hostname or "").lower()
        is_local = hostname in {"127.0.0.1", "localhost", "::1"}
        if not is_local and not self.api_key:
            raise VLMRequestError(
                "远程 API 未配置密钥；请先 export VLM_API_KEY='你的密钥'"
            )

    def complete(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: Optional[int] = None,
    ) -> str:
        self._require_credentials()
        output_limit = self.max_tokens if max_tokens is None else max_tokens
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": output_limit,
            "stream": False,
        }
        if self.enable_thinking is not None:
            payload["enable_thinking"] = self.enable_thinking
        if self.use_json_object:
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "kuavo-elevator-vlm-api/1.0",
        }
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key

        result = None
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(
                self.api_url, data=body, headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    result = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                last_error = VLMRequestError("VLM HTTP %s: %s" % (exc.code, detail))
                if exc.code not in RETRYABLE_HTTP_CODES or attempt >= self.max_retries:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = VLMRequestError("VLM 网络请求失败：%s" % exc)
                if attempt >= self.max_retries:
                    raise last_error from exc
            except json.JSONDecodeError as exc:
                raise VLMRequestError("API 返回的外层响应不是合法 JSON") from exc
            time.sleep(min(2.0, 0.5 * (2**attempt)))

        if result is None:
            raise last_error or VLMRequestError("VLM API 没有返回结果")
        try:
            choice = result["choices"][0]
            message = choice["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VLMRequestError("VLM 响应缺少 choices[0].message.content") from exc
        finish_reason = choice.get("finish_reason", "unknown")
        if finish_reason == "length":
            raise VLMOutputError(
                "VLM 输出达到长度上限并被截断：finish_reason=length, max_tokens=%s"
                % output_limit
            )
        if isinstance(content, str):
            if not content.strip():
                raise VLMOutputError(
                    "VLM 最终 content 为空：finish_reason=%s" % finish_reason
                )
            return content
        if isinstance(content, list):
            texts = [part.get("text", "") for part in content if isinstance(part, dict)]
            joined = "".join(texts)
            if joined.strip():
                return joined
        raise VLMRequestError("VLM 响应 content 类型不受支持或没有文本")

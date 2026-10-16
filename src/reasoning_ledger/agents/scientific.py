"""Shared, provenance-first HTTP access for scientific research tools."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ..canonical import canonical_json

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ScientificToolError(RuntimeError):
    """A scientific provider request or cached response was unusable."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class HttpTransport(Protocol):
    def __call__(
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse: ...


def _now_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def _default_transport(
    url: str, headers: Mapping[str, str], timeout: float
) -> HttpResponse:
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        response = urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
    except urllib.error.HTTPError as error:
        body = error.read(_MAX_RESPONSE_BYTES + 1)
        return HttpResponse(error.code, body, dict(error.headers.items()))
    with response:
        length = response.headers.get("Content-Length")
        if length and int(length) > _MAX_RESPONSE_BYTES:
            raise ScientificToolError("scientific provider response exceeds 8 MiB")
        body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ScientificToolError("scientific provider response exceeds 8 MiB")
        return HttpResponse(response.status, body, dict(response.headers.items()))


class ScientificGateway:
    """Rate-limited, retried, content-addressed access with explicit sources."""

    def __init__(
        self,
        cache_root: Path | str,
        *,
        transport: HttpTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        user_agent: str = "Reasoning/0.1",
    ):
        self.cache_root = Path(cache_root)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.transport = transport or _default_transport
        self.clock = clock
        self.sleeper = sleeper
        self.user_agent = user_agent
        self._rate_lock = threading.Lock()
        self._last_request: dict[str, float] = {}

    @staticmethod
    def _request_url(url: str, parameters: Mapping[str, Any] | None) -> str:
        if not parameters:
            return url
        encoded = urllib.parse.urlencode(parameters, doseq=True)
        separator = "&" if urllib.parse.urlsplit(url).query else "?"
        return f"{url}{separator}{encoded}"

    def _cache_path(self, provider: str, url: str, media: str) -> Path:
        digest = hashlib.sha256(f"{media}\0{url}".encode()).hexdigest()
        safe_provider = "".join(
            character if character.isalnum() or character in "-_" else "-"
            for character in provider.lower()
        ).strip("-") or "source"
        return self.cache_root / safe_provider / f"{digest}.json"

    @staticmethod
    def _load_cache(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _valid_cache(value: dict[str, Any] | None) -> bool:
        return bool(
            value
            and isinstance(value.get("stored_at"), (int, float))
            and isinstance(value.get("retrieved_at"), str)
            and isinstance(value.get("content_sha256"), str)
            and "data" in value
        )

    @staticmethod
    def _store_cache(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _wait_for_provider(self, provider: str, minimum_interval: float) -> None:
        if minimum_interval <= 0:
            return
        with self._rate_lock:
            now = self.clock()
            wait = self._last_request.get(provider, now - minimum_interval) + minimum_interval - now
            if wait > 0:
                self.sleeper(wait)
                now = self.clock()
            self._last_request[provider] = now

    @staticmethod
    def _decode(response: HttpResponse, media: str) -> Any:
        if len(response.body) > _MAX_RESPONSE_BYTES:
            raise ScientificToolError("scientific provider response exceeds 8 MiB")
        if media == "bytes":
            return base64.b64encode(response.body).decode("ascii")
        try:
            text = response.body.decode("utf-8")
        except UnicodeError as error:
            raise ScientificToolError("scientific provider response is not UTF-8") from error
        if media == "text":
            return text
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise ScientificToolError("scientific provider returned invalid JSON") from error

    @staticmethod
    def _retry_after(headers: Mapping[str, str], fallback: float) -> float:
        value = next(
            (raw for key, raw in headers.items() if key.lower() == "retry-after"), None
        )
        try:
            return min(30.0, max(0.0, float(value))) if value is not None else fallback
        except ValueError:
            return fallback

    def request(
        self,
        *,
        provider: str,
        url: str,
        query: Mapping[str, Any] | None = None,
        license: str | None = None,
        media: str = "json",
        ttl_seconds: float = 24 * 60 * 60,
        minimum_interval: float = 0.25,
        timeout: float = 20,
        max_attempts: int = 3,
        allow_stale_on_error: bool = True,
    ) -> dict[str, Any]:
        if media not in {"bytes", "json", "text"}:
            raise ValueError("media must be 'bytes', 'json', or 'text'")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        query_value = dict(query or {})
        request_url = self._request_url(url, query_value)
        cache_path = self._cache_path(provider, request_url, media)
        cached = self._load_cache(cache_path)
        now = self.clock()
        if self._valid_cache(cached) and now - float(cached["stored_at"]) <= ttl_seconds:
            return self._envelope(
                provider=provider,
                url=request_url,
                query=query_value,
                license=license,
                retrieved_at=str(cached["retrieved_at"]),
                content_sha256=str(cached["content_sha256"]),
                data=cached.get("data"),
                cache="hit",
            )

        last_error: Exception | None = None
        for attempt in range(max_attempts):
            self._wait_for_provider(provider, minimum_interval)
            response: HttpResponse | None = None
            try:
                response = self.transport(
                    request_url,
                    {
                        "Accept": (
                            "application/json"
                            if media == "json"
                            else (
                                "application/octet-stream, application/gzip"
                                if media == "bytes"
                                else "text/plain, application/xml"
                            )
                        ),
                        "User-Agent": self.user_agent,
                    },
                    timeout,
                )
                if not 200 <= response.status < 300:
                    raise ScientificToolError(
                        f"{provider} returned HTTP {response.status}", status=response.status
                    )
                data = self._decode(response, media)
                content_sha256 = (
                    hashlib.sha256(response.body).hexdigest()
                    if media == "bytes"
                    else hashlib.sha256(
                        (canonical_json(data) if media == "json" else str(data)).encode("utf-8")
                    ).hexdigest()
                )
                retrieved_at = _now_iso(self.clock())
                self._store_cache(
                    cache_path,
                    {
                        "stored_at": self.clock(),
                        "retrieved_at": retrieved_at,
                        "content_sha256": content_sha256,
                        "data": data,
                    },
                )
                return self._envelope(
                    provider=provider,
                    url=request_url,
                    query=query_value,
                    license=license,
                    retrieved_at=retrieved_at,
                    content_sha256=content_sha256,
                    data=data,
                    cache="miss",
                )
            except (OSError, ScientificToolError) as error:
                last_error = error
                retryable = response is None or response.status == 429 or response.status >= 500
                if retryable and attempt + 1 < max_attempts:
                    delay = 0.5 * (2**attempt)
                    if response is not None:
                        delay = self._retry_after(response.headers, delay)
                    self.sleeper(delay)
                else:
                    break

        if self._valid_cache(cached) and allow_stale_on_error:
            envelope = self._envelope(
                provider=provider,
                url=request_url,
                query=query_value,
                license=license,
                retrieved_at=str(cached["retrieved_at"]),
                content_sha256=str(cached["content_sha256"]),
                data=cached.get("data"),
                cache="stale",
            )
            envelope["source"]["warning"] = f"Live refresh failed: {last_error}"
            return envelope
        raise ScientificToolError(
            str(last_error or f"{provider} request failed"), status=getattr(last_error, "status", None)
        )

    @staticmethod
    def _envelope(
        *,
        provider: str,
        url: str,
        query: Mapping[str, Any],
        license: str | None,
        retrieved_at: str,
        content_sha256: str,
        data: Any,
        cache: str,
    ) -> dict[str, Any]:
        return {
            "source": {
                "provider": provider,
                "url": url,
                "query": dict(query),
                "license": license,
                "retrieved_at": retrieved_at,
                "content_sha256": content_sha256,
                "cache": cache,
            },
            "data": data,
        }

    def get_json(self, **kwargs: Any) -> dict[str, Any]:
        return self.request(media="json", **kwargs)

    def get_text(self, **kwargs: Any) -> dict[str, Any]:
        return self.request(media="text", **kwargs)

    def get_bytes(self, **kwargs: Any) -> dict[str, Any]:
        return self.request(media="bytes", **kwargs)

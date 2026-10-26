"""Bounded text-only OpenRouter transport; credentials never become run state."""

from __future__ import annotations

import asyncio
import codecs
import copy
import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx

from .reasoning_effort import openrouter_efforts as reasoning_efforts

OPENROUTER_URL = "https://openrouter.ai/api/v1"
OUTPUT_RESERVATION_TOKENS = 8192
CATALOG_TTL = 60
CATALOG_RETRY = 5
# OpenRouter's generation statistics accept only its own `gen-` identifiers.
GENERATION_ID = re.compile(r"gen-[0-9A-Za-z-]{1,124}")


class ProviderError(Exception):
    def __init__(self, code: str, *, no_charge: bool = False, diagnostics: dict | None = None):
        super().__init__(code)
        self.code, self.no_charge, self.diagnostics = code, no_charge, diagnostics


# Why OpenRouter found no endpoint (HTTP 404), read from its message into a fixed set of explanations.
UNMATCHED = {
    "data_policy": (
        "OpenRouter found no endpoint for this model that matches the account's data policy.",
        "Free models usually need free endpoints allowed in OpenRouter's privacy settings; also check this organization's "
        "zero-data-retention routing, or select another model.",
    ),
    "parameters": (
        "OpenRouter found no endpoint for this model that accepts this request's tools or parameters.",
        "Select a model whose endpoints support the tools and parameters this topic uses.",
    ),
    "providers": (
        "OpenRouter's provider settings exclude every endpoint for this model.",
        "Check the key's allowed and ignored providers in OpenRouter, or select another model.",
    ),
    "model": (
        "OpenRouter does not recognize this model.",
        "Refresh the model catalog and select an available model.",
    ),
}


def unmatched_reason(message: str) -> str | None:
    text = message.lower()
    if any(word in text for word in ("data policy", "privacy", "zdr", "data collection", "training", "publication")):
        return "data_policy"
    if any(word in text for word in ("tool", "parameter", "requested", "support")):
        return "parameters"
    if "provider" in text:
        return "providers"
    if "model" in text and any(word in text for word in ("not found", "not a valid", "does not exist")):
        return "model"
    return None


def rejection_error(status: int, metadata: object = None) -> ProviderError:
    """Expose static guidance and allowlisted enums, never raw provider text.

    no_charge applies only to an HTTP refusal before streaming starts, not an
    error inside an accepted stream whose usage may be unknown. A refusal of the
    request itself (format, credential, credit, permission, route, size or
    parameters) produced no generation and so no charge; a timeout, rate limit or
    upstream failure may follow provider work and stays uncertain.
    """
    categories = {
        400: (
            "request",
            "The provider rejected the request format or parameters.",
            "Try another compatible model; an administrator can inspect the provider activity record.",
        ),
        401: (
            "authentication",
            "The OpenRouter credential was rejected.",
            "An administrator should replace or enable the organization key.",
        ),
        402: (
            "credit",
            "The provider reported insufficient credit or a spending limit.",
            "Check OpenRouter credit and key spending limits.",
        ),
        403: (
            "permission",
            "The provider denied access to this request.",
            "Check key permissions and the provider's model or content restrictions.",
        ),
        404: (
            "availability",
            "OpenRouter found no available endpoint for this model and request.",
            "Check the organization's OpenRouter privacy and provider settings, or select another model.",
        ),
        408: (
            "timeout",
            "The provider timed out before returning a response.",
            "Check provider activity before retrying; this submission may have incurred a charge.",
        ),
        413: ("context", "The provider refused the size of the request.", "Reduce pinned context or choose a larger-context model."),
        422: (
            "parameters",
            "The selected provider does not accept these parameters.",
            "Select another model or route with the required capabilities.",
        ),
        429: ("rate_limit", "The provider rate limit was reached.", "Wait, check upstream limits, or select another model."),
        502: ("upstream", "The upstream model provider failed.", "Check provider activity before retrying or choosing another model."),
        503: (
            "availability",
            "No eligible provider was available under this request's routing rules.",
            "Try another model or retry later; check provider activity first.",
        ),
    }
    category, message, action = categories.get(
        status, ("provider", "The provider rejected this submission.", "Check OpenRouter activity and provider status before retrying.")
    )
    diagnostics = {"http_status": status, "category": category, "message": message, "action": action}
    if status == 403 and isinstance(metadata, dict) and metadata.get("restriction") in {"region", "privacy", "model"}:
        restriction = metadata["restriction"]
        diagnostics["restriction"] = restriction
        diagnostics["action"] = {
            "region": "Check the key's regional API eligibility and the model's permitted countries; no region fallback was attempted.",
            "privacy": "Check OpenRouter privacy/ZDR eligibility for this model. Required privacy policies are not relaxed automatically.",
            "model": "Check the OpenRouter key and account's permission to use this model; choose another permitted model if necessary.",
        }[restriction]
    if status == 404 and isinstance(metadata, dict) and metadata.get("unmatched") in UNMATCHED:
        diagnostics["unmatched"] = metadata["unmatched"]
        diagnostics["message"], diagnostics["action"] = UNMATCHED[metadata["unmatched"]]
    if status == 402 and isinstance(metadata, dict):
        source, reason = metadata.get("limit_source"), metadata.get("reason")
        if source in ("openrouter_key_limit", "openrouter_credits", "openrouter_in_flight_budget"):
            diagnostics["limit_source"] = source
        if reason in ("in_flight_budget_exhausted", "weight_exceeds_budget"):
            diagnostics["reason"] = reason
        if source == "openrouter_key_limit":
            diagnostics.update(
                message="The OpenRouter key's spending limit was reached.", action="Check or raise the key's spending limit."
            )
        elif source == "openrouter_in_flight_budget":
            diagnostics.update(
                message="OpenRouter's concurrent spending allowance is temporarily full.",
                action="Wait for running calls and recent charges to settle, then retry explicitly.",
            )
        elif source == "openrouter_credits" and reason == "weight_exceeds_budget":
            diagnostics.update(
                message="The request's estimated maximum cost exceeds OpenRouter's available spending allowance.",
                action="Reduce the output allowance or prompt size, or add OpenRouter credit.",
            )
    return ProviderError(
        "provider_rejected",
        no_charge=status in {400, 401, 402, 403, 404, 413, 422},
        diagnostics=diagnostics,
    )


PROTOCOL_STAGES = {
    "stream_framing": "stream framing or JSON",
    "generation": "generation identifier",
    "usage": "usage accounting",
    "choices": "response choices or finish reason",
    "content": "assistant content",
    "reasoning": "reasoning metadata",
    "tool_calls": "streamed tool calls",
    "tool_validation": "completed tool-call structure",
    "text_encoding": "response text encoding",
}

# Wire framing overhead is not an output-token budget. Reasoning providers can
# emit large events and many small SSE frames before returning their answer.
MAX_STREAM_BYTES = 64 * 1024 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024
PROTOCOL_RULES = {"content_type", "stream_size", "event_size", "invalid_json", "invalid_utf8", "response_structure"}
# Closed names for the response field that failed validation, with the JSON
# shape the parser accepts there. Diagnostics never echo provider values.
PROTOCOL_FIELDS = {
    "reasoning": "a string or null",
    "reasoning_content": "a string or null",
    "reasoning_details": "an array",
    "reasoning_details[]": "an object",
    "reasoning_details[].type": "a non-empty string",
    "reasoning_details[].index": "a non-negative integer",
    "reasoning_details[].id": "a string or null",
    "reasoning_details[].format": "a string or null",
    "reasoning_details[].text": "a string or null",
    "reasoning_details[].summary": "a string or null",
    "reasoning_details[].data": "a string or null",
    "reasoning_details[].signature": "a string or null",
}
JSON_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
PROTOCOL_ACTION = (
    "No automatic retry was made, and a charge may have occurred. Reconcile the call's generation ID with "
    "OpenRouter activity, then retry explicitly."
)


def json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    return {dict: "object", list: "array", str: "string"}.get(type(value), "number")


class StreamLines:
    """Bound incremental UTF-8 line framing without repeatedly scanning a growing buffer."""

    def __init__(self):
        self.parts, self.pending_bytes = [], 0

    def feed(self, fragment, received_bytes):
        pieces = fragment.split("\n")
        for index, piece in enumerate(pieces):
            self.pending_bytes += len(piece.encode("utf8"))
            if self.pending_bytes > MAX_EVENT_BYTES:
                raise ProviderError("provider_protocol",
                                    diagnostics=protocol_diagnostics("stream_framing", "event_size", received_bytes))
            if piece:
                self.parts.append(piece)
            if index < len(pieces) - 1:
                line = "".join(self.parts).rstrip("\r")
                self.parts, self.pending_bytes = [], 0
                yield line


def protocol_diagnostics(stage: str, rule=None, received_bytes=None, field=None, value_type=None) -> dict:
    stage = stage if stage in PROTOCOL_STAGES else "stream_framing"
    result = {
        "category": "protocol", "protocol_stage": stage,
        "message": "The response could not be parsed: " + PROTOCOL_STAGES[stage] + ".",
        "action": PROTOCOL_ACTION,
    }
    if rule in PROTOCOL_RULES:
        result["protocol_rule"] = rule
        if rule in {"stream_size", "event_size"}:
            result["message"] = "Provider transport exceeded its safety allowance: " + rule.replace("_", " ") + "."
    if isinstance(field, str) and field in PROTOCOL_FIELDS:
        result["protocol_field"] = field
        received = ""
        if isinstance(value_type, str) and value_type in JSON_TYPES:
            result["protocol_value_type"] = value_type
            received = "; received " + value_type
        result["message"] = (
            "The response could not be parsed: " + PROTOCOL_STAGES[stage] + " field " + field
            + " must be " + PROTOCOL_FIELDS[field] + received + "."
        )
    if type(received_bytes) is int and 0 <= received_bytes <= 2**63 - 1:
        result["received_bytes"] = received_bytes
    return result


def safe_diagnostics(value: object) -> dict | None:
    if (isinstance(value, dict) and value.get("category") == "protocol"
            and isinstance(value.get("protocol_stage"), str) and value["protocol_stage"] in PROTOCOL_STAGES):
        return protocol_diagnostics(value["protocol_stage"], value.get("protocol_rule"), value.get("received_bytes"),
                                    value.get("protocol_field"), value.get("protocol_value_type"))
    status = value.get("http_status") if isinstance(value, dict) else None
    return rejection_error(status, value).diagnostics if type(status) is int and 300 <= status <= 599 else None


def stream_error_diagnostics(error: object) -> dict | None:
    status = error.get("code") if isinstance(error, dict) else None
    return rejection_error(status, error.get("metadata")).diagnostics if type(status) is int and 300 <= status <= 599 else None


async def response_rejection(response: httpx.Response) -> ProviderError:
    """Read a bounded error envelope and discard non-allowlisted values."""
    metadata = None
    if response.status_code in {402, 403, 404}:
        try:
            async with asyncio.timeout(5):
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk[: 65537 - len(body)])
                    if len(body) > 65536:
                        break
            if len(body) <= 65536:
                value = json.loads(body)
                error = value.get("error") if isinstance(value, dict) else None
                metadata = error.get("metadata") if isinstance(error, dict) else None
                if response.status_code == 404 and isinstance(error, dict):
                    metadata = {"unmatched": unmatched_reason(str(error.get("message", "")))}
                if response.status_code == 403 and isinstance(error, dict):
                    text = str(error.get("message", "")).lower()
                    restriction = "region" if any(word in text for word in ("region", "country", "geograph")) else (
                        "privacy" if any(word in text for word in ("privacy", "zdr", "data policy", "data collection")) else (
                        "model" if any(word in text for word in ("model access", "permission", "not allowed")) else None))
                    metadata = {"restriction": restriction}
        except (TimeoutError, httpx.HTTPError, ValueError, RecursionError):
            # Optional diagnostic text cannot undo the known HTTP refusal.
            pass
    return rejection_error(response.status_code, metadata)


def provider_origin(value: str) -> str:
    if any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in value):
        raise ValueError("invalid provider endpoint")
    url = urlsplit(value)
    if value.rstrip("/") == OPENROUTER_URL:
        return OPENROUTER_URL
    if (
        url.scheme == "http"
        and url.hostname in {"127.0.0.1", "localhost", "::1"}
        and not url.username
        and not url.password
        and not url.query
        and not url.fragment
        and url.path.rstrip("/") in {"", "/api/v1"}
    ):
        _ = url.port
        return value.rstrip("/")
    raise ValueError("OpenRouter endpoint must be the canonical HTTPS endpoint or an HTTP loopback test fixture")


def money(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise ProviderError("provider_protocol")
    try:
        result = Decimal(str(value))
        if not result.is_finite() or not 0 <= result <= 10**9:
            raise ProviderError("provider_protocol")
        return result
    except InvalidOperation as exc:
        raise ProviderError("provider_protocol") from exc


def microusd(value: object) -> int:
    return int((money(value) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def optional_price(value: object, *, per_million: bool = False) -> float | None:
    if value is None:
        return None
    try:
        amount = money(value)
    except ProviderError:
        return None
    return float(amount * (1_000_000 if per_million else 1))


def bounded_strings(value: object, *, maximum: int = 100, length: int = 100) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({item for item in value if isinstance(item, str) and 1 <= len(item) <= length})[:maximum]


@dataclass(frozen=True)
class Quote:
    model: str
    prompt: str
    completion: str
    request: str
    context_length: int
    max_completion_tokens: int
    pricing: dict[str, str] | None = None
    supports_tools: bool = False
    routing: dict | None = None
    supports_images: bool = False
    reasoning_efforts: tuple[str, ...] = ()

    @property
    def free(self) -> bool:
        """Every listed price is zero, so no call on this route can be charged."""
        try:
            return all(money(price) == 0 for price in (self.prompt, self.completion, self.request, *(self.pricing or {}).values()))
        except ProviderError:
            return False

    def route(self):
        policy = self.routing or {"strategy": "cheapest", "zdr": False, "region": "global"}
        route = {"allow_fallbacks": True, "require_parameters": True}
        if policy["strategy"] == "cheapest":
            route.update(sort="price", max_price={"prompt": float(money(self.prompt) * 1_000_000),
                "completion": float(money(self.completion) * 1_000_000)})
        elif policy["strategy"] in {"latency", "throughput"}:
            route["sort"] = policy["strategy"]
        if policy["zdr"]:
            route.update(zdr=True, data_collection="deny")
        return route

    def origin(self, default):
        region = (self.routing or {}).get("region", "global")
        return {"us": "https://us.openrouter.ai/api/v1", "eu": "https://eu.openrouter.ai/api/v1"}.get(region, default)

    def reservation(self, messages: list[dict], output_tokens: int) -> int:
        # Conservative token/serialization allowance, not an upstream invoice guarantee.
        prompt_bound = sum(len(str(message.get("content") or "").encode("utf8")) + 64 for message in messages) + 256
        if output_tokens > self.max_completion_tokens or prompt_bound + output_tokens > self.context_length:
            raise ProviderError("context_limit", no_charge=True)
        # Context capacity uses a UTF-8 bound; billing uses an estimate rather
        # than one token per byte with a second multiplier. Not an invoice cap.
        estimated_input = (prompt_bound + 2) // 3
        return microusd(
            estimated_input * money(self.prompt) + output_tokens * money(self.completion) + money(self.request)
        ) + self.image_reservation(messages)

    def image_reservation(self, messages):
        images = sum(
            1
            for message in messages
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if isinstance(part, dict) and part.get("type") == "image_url"
        )
        return microusd(images * money((self.pricing or {}).get("image", "0")))

    def record(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Usage:
    cost_microusd: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    tool_tokens: int | None = None

    @classmethod
    def parse(cls, body: dict) -> Usage:
        counts = [body.get("prompt_tokens"), body.get("completion_tokens")]
        if any(not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 10**9 for count in counts):
            raise ProviderError("provider_protocol")

        def count(value):
            return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**9 else None

        prompt_details = body.get("prompt_tokens_details") if isinstance(body.get("prompt_tokens_details"), dict) else {}
        completion_details = body.get("completion_tokens_details") if isinstance(body.get("completion_tokens_details"), dict) else {}
        return cls(
            microusd(body.get("cost")),
            *counts,
            count(prompt_details.get("cached_tokens")),
            count(completion_details.get("reasoning_tokens")),
            count(body.get("total_tokens")),
            count(body.get("tool_tokens")),
        )


_TLS_CONTEXT = None


def tls_context():
    """One CA-verifying TLS context per process, as httpx builds without environment trust.

    Building it loads the CA bundle (milliseconds of CPU); a client per
    provider call or catalog read otherwise repeats that work every time.
    """
    global _TLS_CONTEXT
    if _TLS_CONTEXT is None:
        _TLS_CONTEXT = httpx.create_ssl_context(trust_env=False)
    return _TLS_CONTEXT


@dataclass(frozen=True)
class Chunk:
    text: str = ""
    generation_id: str | None = None
    usage: Usage | None = None
    finish_reason: str | None = None


class OpenRouter:
    def __init__(self, base_url: str = OPENROUTER_URL, *, transport: httpx.AsyncBaseTransport | None = None, clock=time.monotonic):
        self.base_url, self.transport = provider_origin(base_url), transport
        self._clock = clock
        self._routing_cache = None
        self._routing_lock = asyncio.Lock()
        self._prices_cache = None
        self._prices_lock = asyncio.Lock()
        self._prices_retry_at = 0
        self.prices_fetched_at = None
        self.catalog_models: list[str] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self.transport,
            verify=tls_context(),
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(30, connect=10),
            limits=httpx.Limits(max_connections=4),
        )

    @staticmethod
    async def _bounded(response: httpx.Response, maximum: int) -> bytes:
        result = bytearray()
        async for chunk in response.aiter_bytes():
            result.extend(chunk)
            if len(result) > maximum:
                raise ProviderError("provider_protocol")
        return bytes(result)

    async def quotes(self, models: list[str]) -> list[Quote]:
        try:
            async with self.client() as client, client.stream("GET", self.base_url + "/models") as response:
                if response.status_code != 200:
                    raise ProviderError("catalog_unavailable", no_charge=True)
                body = json.loads(await self._bounded(response, 8 * 1024 * 1024))
            catalog = {row["id"]: row for row in body["data"] if isinstance(row, dict) and isinstance(row.get("id"), str)}
            self.catalog_models = [
                model for model, row in catalog.items()
                if 1 <= len(model) <= 200 and isinstance(row.get("architecture"), dict)
                and row["architecture"].get("output_modalities") == ["text"]
                and "text" in bounded_strings(row["architecture"].get("input_modalities"), maximum=16, length=50)
            ]
            result = []
            for model in models:
                row = catalog.get(model)
                if row is None:
                    raise ProviderError("model_unavailable", no_charge=True)
                architecture = row.get("architecture", {})
                if architecture.get("output_modalities") != ["text"] or "text" not in architecture.get("input_modalities", []):
                    raise ProviderError("model_not_text", no_charge=True)
                if not {"max_tokens", "max_completion_tokens"}.intersection(row.get("supported_parameters", [])):
                    raise ProviderError("model_output_limit_unsupported", no_charge=True)
                pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
                rates = [str(money(pricing[field])) for field in ("prompt", "completion")]
                rates.append(str(money(pricing.get("request", "0"))))
                context = row["context_length"]
                maximum = row.get("top_provider", {}).get("max_completion_tokens") or 8192
                if (
                    not isinstance(context, int)
                    or isinstance(context, bool)
                    or not isinstance(maximum, int)
                    or isinstance(maximum, bool)
                    or context < 1
                    or maximum < 1
                ):
                    raise ProviderError("provider_protocol")
                schedule = {
                    field: str(money(value))
                    for field, value in pricing.items()
                    if field
                    in {
                        "prompt",
                        "completion",
                        "request",
                        "input_cache_read",
                        "input_cache_write",
                        "internal_reasoning",
                        "image",
                        "web_search",
                    }
                    and optional_price(value) is not None
                }
                result.append(
                    Quote(
                        model,
                        *rates,
                        context,
                        maximum,
                        schedule,
                        "tools" in row.get("supported_parameters", []),
                        supports_images="image" in bounded_strings(architecture.get("input_modalities"), maximum=16, length=50),
                        reasoning_efforts=tuple(reasoning_efforts(row)),
                    )
                )
            return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise ProviderError("catalog_unavailable", no_charge=True) from exc

    async def routing_availability(self):
        async with self._routing_lock:
            return await self._routing_availability()

    async def _routing_availability(self):
        """Public, optional availability metadata; never a promise about a chosen request route."""
        if self._routing_cache and self._routing_cache[0] > self._clock():
            return self._routing_cache[1]

        async def ids(parameters):
            try:
                async with self.client() as client, client.stream("GET", self.base_url + "/models", params=parameters,
                                                                 timeout=10) as response:
                    if response.status_code != 200:
                        return None
                    body = json.loads(await self._bounded(response, 8 * 1024 * 1024))
                if not isinstance(body.get("data"), list):
                    return None
                return {row["id"] for row in body["data"] if isinstance(row, dict) and isinstance(row.get("id"), str)}
            except (httpx.HTTPError, ProviderError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
                return None

        result = dict(zip(("zdr", "us", "eu"), await asyncio.gather(
            ids({"zdr": "true"}), ids({"region": "us"}), ids({"region": "eu"})
        ), strict=True))
        self._routing_cache = (self._clock() + 300, result)
        return result

    async def prices(self, *, include_routing=False) -> list[dict]:
        """Share bounded public metadata across tabs; admission quotes stay fresh."""
        async with self._prices_lock:
            now = self._clock()
            if self._prices_cache is not None and self._prices_cache[0] > now:
                result = self._prices_cache[1]
            else:
                if self._prices_retry_at > now:
                    raise ProviderError("catalog_unavailable", no_charge=True)
                try:
                    result = await self._prices()
                except ProviderError:
                    self._prices_retry_at = self._clock() + CATALOG_RETRY
                    raise
                self._prices_cache = (self._clock() + CATALOG_TTL, result)
                self.prices_fetched_at = int(time.time())
            result = copy.deepcopy(result)
        if include_routing:
            availability = await self.routing_availability()
            for model in result:
                model["routing_availability"] = {
                    "zdr": model["id"] in availability["zdr"] if availability["zdr"] is not None else None,
                    "regions": [region.upper() for region in ("us", "eu")
                                if availability[region] is not None and model["id"] in availability[region]],
                    "source": "OpenRouter filtered public catalog; availability only, not enforced request routing",
                }
        return result

    async def _prices(self) -> list[dict]:
        """Fetch and validate public metadata without credentials or inference."""
        try:
            async with self.client() as client, client.stream("GET", self.base_url + "/models") as response:
                if response.status_code != 200:
                    raise ProviderError("catalog_unavailable", no_charge=True)
                body = json.loads(await self._bounded(response, 8 * 1024 * 1024))
            if not isinstance(body.get("data"), list) or len(body["data"]) > 10000:
                raise ProviderError("catalog_unavailable", no_charge=True)
            result = []
            for row in body["data"]:
                if not isinstance(row, dict):
                    continue
                architecture = row.get("architecture") if isinstance(row.get("architecture"), dict) else {}
                inputs = bounded_strings(architecture.get("input_modalities"), maximum=16, length=50)
                outputs = bounded_strings(architecture.get("output_modalities"), maximum=16, length=50)
                if outputs != ["text"] or "text" not in inputs:
                    continue
                model, context = row.get("id"), row.get("context_length")
                if (
                    not isinstance(model, str)
                    or not 1 <= len(model) <= 200
                    or not isinstance(context, int)
                    or isinstance(context, bool)
                    or context < 1
                ):
                    continue
                pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
                parameters = bounded_strings(row.get("supported_parameters"), length=100)
                top_provider = row.get("top_provider") if isinstance(row.get("top_provider"), dict) else {}
                maximum = top_provider.get("max_completion_tokens")
                description = row.get("description") if isinstance(row.get("description"), str) else ""
                modality = architecture.get("modality") if isinstance(architecture.get("modality"), str) else None
                result.append(
                    {
                        "id": model,
                        "canonical_slug": row["canonical_slug"] if isinstance(row.get("canonical_slug"), str)
                        and 1 <= len(row["canonical_slug"]) <= 200 else None,
                        "name": (row.get("name") if isinstance(row.get("name"), str) else model)[:300],
                        "provider": model.partition("/")[0],
                        "weights_repository": row.get("hugging_face_id") if isinstance(row.get("hugging_face_id"), str)
                        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", row["hugging_face_id"]) else None,
                        "description": description.strip()[:1200],
                        "modality": modality[:100] if modality else None,
                        "input_modalities": inputs,
                        "output_modalities": outputs,
                        "supported_parameters": parameters,
                        "supports_tools": "tools" in parameters,
                        "supports_reasoning": bool({"reasoning", "include_reasoning"}.intersection(parameters)),
                        "reasoning_efforts": reasoning_efforts(row),
                        "input_usd_per_million": optional_price(pricing.get("prompt"), per_million=True),
                        "output_usd_per_million": optional_price(pricing.get("completion"), per_million=True),
                        "cache_read_usd_per_million": optional_price(pricing.get("input_cache_read"), per_million=True),
                        "cache_write_usd_per_million": optional_price(pricing.get("input_cache_write"), per_million=True),
                        "reasoning_usd_per_million": optional_price(pricing.get("internal_reasoning"), per_million=True),
                        "request_usd": optional_price(pricing.get("request")),
                        "image_usd": optional_price(pricing.get("image")),
                        "web_search_usd": optional_price(pricing.get("web_search")),
                        "context_length": context,
                        "max_completion_tokens": (
                            maximum if isinstance(maximum, int) and not isinstance(maximum, bool) and maximum > 0 else None
                        ),
                    }
                )
            return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise ProviderError("catalog_unavailable", no_charge=True) from exc

    async def generation(self, key: str, generation: str, *, origin: str | None = None) -> dict | None:
        """The provider's recorded charge for one generation; None while it has no record.

        A read of OpenRouter's generation statistics, never a model request.
        """
        if not GENERATION_ID.fullmatch(generation):
            raise ProviderError("provider_protocol")
        if any(ord(char) < 32 or ord(char) > 126 for char in key):
            raise ProviderError("provider_key_invalid", no_charge=True)
        try:
            async with (
                self.client() as client,
                client.stream(
                    "GET",
                    (origin or self.base_url) + "/generation",
                    params={"id": generation},
                    headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
                ) as response,
            ):
                if response.status_code == 404:
                    return None
                if response.status_code != 200:
                    raise ProviderError("generation_unavailable")
                body = json.loads(await self._bounded(response, 1024 * 1024), parse_float=Decimal)
            data = body["data"]
            if not isinstance(data, dict) or data.get("id") != generation:
                raise ProviderError("provider_protocol")

            def count(value):
                return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**9 else None

            return {
                "cost_microusd": microusd(data["total_cost"]),
                "prompt_tokens": count(data.get("tokens_prompt")),
                "completion_tokens": count(data.get("tokens_completion")),
                "cached_tokens": count(data.get("native_tokens_cached")),
                "reasoning_tokens": count(data.get("native_tokens_reasoning")),
                "cancelled": data.get("cancelled") is True,
            }
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise ProviderError("generation_unavailable" if isinstance(exc, httpx.HTTPError) else "provider_protocol") from exc

    async def stream(self, key: str, quote: Quote, messages: list[dict], output_tokens: int | None) -> AsyncIterator[Chunk]:
        payload = {
            "model": quote.model,
            "messages": messages,
            "stream": True,
            "usage": {"include": True},
            "provider": quote.route(),
        }
        if output_tokens is not None:
            payload["max_completion_tokens"] = output_tokens
        if any(ord(char) < 32 or ord(char) > 126 for char in key):
            raise ProviderError("provider_key_invalid", no_charge=True)
        try:
            async with (
                self.client() as client,
                client.stream(
                    "POST",
                    quote.origin(self.base_url) + "/chat/completions",
                    headers={"Authorization": "Bearer " + key, "Accept": "text/event-stream"},
                    json=payload,
                    timeout=httpx.Timeout(1800, connect=10),
                ) as response,
            ):
                if response.status_code != 200:
                    raise await response_rejection(response)
                if not response.headers.get("content-type", "").lower().startswith("text/event-stream"):
                    raise ProviderError("provider_protocol", diagnostics=protocol_diagnostics("stream_framing", "content_type"))
                lines, event, event_bytes, total = StreamLines(), [], 0, 0
                decoder = codecs.getincrementaldecoder("utf8")()
                async for raw in response.aiter_bytes():
                    total += len(raw)
                    if total > MAX_STREAM_BYTES:
                        raise ProviderError("provider_protocol", diagnostics=protocol_diagnostics("stream_framing", "stream_size", total))
                    for line in lines.feed(decoder.decode(raw), total):
                        if line.startswith("data:"):
                            event.append(line[5:].lstrip(" "))
                            event_bytes += len(event[-1].encode("utf8")) + 1
                            if event_bytes > MAX_EVENT_BYTES:
                                raise ProviderError("provider_protocol",
                                                    diagnostics=protocol_diagnostics("stream_framing", "event_size", total))
                        elif not line and event:
                            data, event = "\n".join(event), []
                            event_bytes = 0
                            if data == "[DONE]":
                                return
                            body = json.loads(data, parse_float=Decimal)
                            if not isinstance(body, dict) or "error" in body:
                                details = body.get("error") if isinstance(body, dict) else None
                                raise ProviderError("provider_stream_error", diagnostics=stream_error_diagnostics(details))
                            generation = body.get("id")
                            if generation is not None and (
                                not isinstance(generation, str) or not re.fullmatch(r"(?:gen-|chatcmpl-)[A-Za-z0-9_-]{1,180}", generation)
                            ):
                                raise ProviderError("provider_protocol")
                            usage = Usage.parse(body["usage"]) if isinstance(body.get("usage"), dict) else None
                            text, finish = "", None
                            for choice in body.get("choices", []):
                                if choice.get("index", 0) != 0:
                                    raise ProviderError("provider_protocol")
                                delta = choice.get("delta", {})
                                if delta.get("tool_calls"):
                                    raise ProviderError("unsupported_tool_call")
                                content = delta.get("content")
                                if content is not None and not isinstance(content, str):
                                    raise ProviderError("provider_protocol")
                                if content is not None:
                                    # JSON escapes can represent unpaired UTF-16
                                    # surrogates even when the wire bytes are
                                    # valid UTF-8. Never yield text SQLite cannot
                                    # store; preserve all valid Unicode unchanged.
                                    content.encode("utf8")
                                text += content or ""
                                if choice.get("finish_reason") is not None:
                                    finish = choice["finish_reason"]
                                    if finish not in {"stop", "length", "content_filter", "tool_calls", "function_call", "error"}:
                                        raise ProviderError("provider_protocol")
                            yield Chunk(text, generation, usage, finish)
                raise ProviderError("provider_incomplete")
        except (httpx.HTTPError, UnicodeError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise ProviderError("provider_network" if isinstance(exc, httpx.HTTPError) else "provider_protocol") from exc

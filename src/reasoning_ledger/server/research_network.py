"""Bounded public HTTPS reads with checked DNS, pinned sockets and no credentials."""

from __future__ import annotations

import asyncio
import re
import ssl
from contextlib import suppress
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import certifi
import h11

from .webhook_transport import endpoint, public_address, resolve

WEB_MAXIMUM = 1024 * 1024
# Matches ScientificGateway's response bound: long arXiv HTML papers and
# source archives routinely exceed 1 MiB.
SCHOLARLY_MAXIMUM = 8 * 1024 * 1024
MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}")
TEXT_MEDIA = {"text/html", "text/plain", "application/json", "application/xml", "application/atom+xml", "text/xml"}
DOCUMENT_ADVICE = (
    "For an arXiv paper use arxiv_read with queries, or arxiv_source_list and arxiv_source_read. For another PDF, "
    "ask a member to attach it and read its extracted text with media_read."
)


class SourceRefused(ValueError):
    """A refused public read, with a model-readable reason and next step."""

    def __init__(self, code, message, action):
        self.code, self.action = code, action
        super().__init__(message)

    def explanation(self):
        return f"{self} {self.action}"


class SourceError(SourceRefused):
    """A non-200 HTTP response. Redirects are reported, never followed."""

    def __init__(self, status, headers=None, url=None):
        self.status = status
        self.location = self.retry_after = None
        headers = headers or {}
        if 300 <= status < 400 and url and headers.get("location"):
            target = urljoin(url, headers["location"])
            if len(target) <= 2048 and target.startswith("https://") and all(33 <= ord(c) <= 126 for c in target):
                self.location = target
        if headers.get("retry-after", "").isdigit():
            self.retry_after = min(int(headers["retry-after"]), 3600)
        if status == 429:
            code, action = "source_rate_limited", "Wait before retrying; do not repeat the request immediately." + (
                f" The source asked for {self.retry_after} seconds." if self.retry_after is not None else "")
        elif status == 404:
            code, action = "source_not_found", "Check the URL or identifier and its version; a search or lookup tool can find it."
        elif 300 <= status < 400:
            code = "source_redirected"
            action = (f"The source redirects to {self.location}; read that URL if it is the intended source."
                      if self.location else "Read the final URL directly; redirects are not followed.")
        elif status in {401, 403}:
            code, action = "source_access_denied", "The source requires access that public reads do not have; use another source."
        elif status in {406, 415}:
            code, action = "source_media_unsupported", "This reader accepts text, HTML, JSON or XML only. " + DOCUMENT_ADVICE
        else:
            code, action = "source_http_error", "Retry later or use an alternate source."
        super().__init__(code, f"The public source returned HTTP {status}.", action)


async def public_get(value: str, *, headers=None, maximum=WEB_MAXIMUM):
    for attempt in range(2):
        try:
            return await public_get_once(value, headers=headers, maximum=maximum)
        except (OSError, TimeoutError, SourceError) as exc:
            if attempt or isinstance(exc, SourceError) and exc.status not in {429, 502, 503, 504}:
                raise
            await asyncio.sleep(0.5)


async def public_get_once(value: str, *, headers=None, maximum=WEB_MAXIMUM):
    url = urlsplit(value)
    if url.fragment or any(ord(c) < 33 or ord(c) > 126 for c in value) or len(value) > 4096:
        raise SourceRefused(
            "source_url_invalid", "The source URL must be at most 4096 printable ASCII characters without a #fragment.",
            "Remove the fragment and percent-encode spaces or non-ASCII characters.",
        )
    try:
        # Reuse the strict public hostname validator; query is allowed only in the request target.
        checked = endpoint(url._replace(query="").geturl())
    except ValueError:
        raise SourceRefused(
            "source_url_invalid", "Public reads require an https:// URL on port 443 with a public hostname and no credentials.",
            "Use the https:// form of the URL; private, local and credentialed addresses are never read.",
        ) from None
    addresses = await resolve(checked.hostname)
    if not addresses or not all(public_address(item) for item in addresses):
        raise SourceRefused(
            "source_address_refused", "The source hostname must resolve exclusively to public addresses.",
            "Use a publicly hosted copy of the source.",
        )
    context = ssl.create_default_context(cafile=certifi.where())
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            addresses[0],
            443,
            ssl=context,
            server_hostname=checked.hostname,
            ssl_handshake_timeout=5,
            ssl_shutdown_timeout=1,
            limit=16 * 1024,
        ),
        8,
    )
    try:
        connection = h11.Connection(h11.CLIENT, max_incomplete_event_size=16 * 1024)
        target = (url.path or "/") + ("?" + url.query if url.query else "")
        request_headers = {
            "Host": checked.netloc,
            "User-Agent": "Reasoning/0.1 research",
            "Accept": "text/*, application/json",
            "Accept-Encoding": "identity",
            "Connection": "close",
            **(headers or {}),
        }
        writer.write(connection.send(h11.Request(method="GET", target=target.encode("ascii"), headers=list(request_headers.items()))))
        writer.write(connection.send(h11.EndOfMessage()))
        await asyncio.wait_for(writer.drain(), 3)
        body, response = bytearray(), None
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            event = connection.next_event()
            if event is h11.NEED_DATA:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise SourceRefused("source_timeout", "The source did not finish responding within 15 seconds.",
                                        "Retry later or use an alternate source.")
                connection.receive_data(await asyncio.wait_for(reader.read(8192), remaining))
            elif isinstance(event, h11.Response):
                response = event
                # Redirects never get credentials and never bypass another DNS/host check.
                if response.status_code != 200:
                    raise SourceError(response.status_code, {key.decode("latin-1").lower(): item.decode("latin-1")
                                                             for key, item in response.headers}, value)
            elif isinstance(event, h11.Data):
                body.extend(event.data)
                if len(body) > maximum:
                    raise SourceRefused(
                        "source_too_large", f"The source exceeds this reader's {maximum // (1024 * 1024)} MiB limit.",
                        "Use a narrower reader: arxiv_read with queries for arXiv HTML, arxiv_source_read for its TeX, "
                        "or a smaller page.",
                    )
            elif isinstance(event, h11.EndOfMessage):
                if response is None:
                    raise SourceRefused("source_incomplete", "The source returned no HTTP response.", "Retry later.")
                return bytes(body), {k.decode().lower(): v.decode() for k, v in response.headers}
            elif isinstance(event, h11.ConnectionClosed):
                raise SourceRefused("source_incomplete", "The source closed the connection before the complete response.",
                                    "Retry later or use an alternate source.")
    finally:
        writer.close()
        with suppress(OSError, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), 2)


def scholarly_transport(url, headers, timeout):
    """One pinned read for ScientificGateway, which owns retries, pacing and stale-cache fallback.

    Rate-limit and server-error statuses are returned to the gateway so it can
    honor Retry-After; other statuses keep their actionable SourceError.
    """
    from ..agents.scientific import HttpResponse

    try:
        body, response_headers = asyncio.run(public_get_once(url, maximum=SCHOLARLY_MAXIMUM))
    except SourceError as exc:
        if exc.status == 429 or exc.status >= 500:
            return HttpResponse(exc.status, b"", {"retry-after": str(exc.retry_after)} if exc.retry_after is not None else {})
        raise
    return HttpResponse(200, body, response_headers)


class SourceText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.ignored = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "svg", "noscript"}:
            self.ignored += 1
        elif not self.ignored and tag in {"p", "div", "br", "h1", "h2", "h3", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "svg", "noscript"}:
            self.ignored = max(0, self.ignored - 1)

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def source_text(body, headers):
    media = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media not in TEXT_MEDIA:
        named = media if MEDIA_TYPE.fullmatch(media) else "an unrecognized media type"
        raise SourceRefused(
            "source_media_unsupported",
            f"The source is {named}, which is not supported text; this reader returns text, HTML, JSON or XML only.",
            DOCUMENT_ADVICE,
        )
    text = body.decode("utf8", errors="replace")
    if media == "text/html":
        parser = SourceText()
        parser.feed(text)
        text = "".join(parser.parts)
    return text

"""Bounded subprocess entry point for PDF text extraction."""

import io
import json
import logging
import signal
import sys
import time

MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_PAGES = 200
# The parent discards a worker that passes its 12-second timeout or the hard CPU limit, one second after this soft one.
# Extraction therefore stops after CPU_SECONDS of processor time or WALL_SECONDS, whichever comes first, and reports
# the pages it has read. It checks before each page; a signal at either limit also interrupts a page in progress.
CPU_SECONDS = 8
WALL_SECONDS = 10


class TimeLimit(BaseException):
    """Extraction ran out of time. A BaseException, so pypdf's own `except Exception` handlers let it through."""


def stop(signum, frame):
    """Stop extraction once, at the first limit reached; later signals are ignored while the result is written."""
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
    signal.signal(signal.SIGXCPU, signal.SIG_IGN)
    raise TimeLimit


def arm():
    """Interrupt extraction at the soft CPU limit or after WALL_SECONDS; return the deadline `extract` checks."""
    if hasattr(signal, "SIGXCPU") and hasattr(signal, "setitimer"):
        signal.signal(signal.SIGXCPU, stop)
        signal.signal(signal.SIGALRM, stop)
        signal.setitimer(signal.ITIMER_REAL, WALL_SECONDS)
    return CPU_SECONDS, time.monotonic() + WALL_SECONDS


def extract(body, deadline=None):
    """Bounded page text. With `deadline`, a (process CPU seconds, monotonic time) pair, no page starts after it."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body), strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        return {"status": "unavailable", "error": "password_required", "text": "", "pages_read": 0}
    total = len(reader.pages)
    parts, size, read = [], 0, 0
    truncated = total > MAX_PAGES
    error = None
    try:
        for index in range(min(total, MAX_PAGES)):
            if deadline and (time.process_time() >= deadline[0] or time.monotonic() >= deadline[1]):
                raise TimeLimit
            try:
                text = reader.pages[index].extract_text() or ""
            except Exception:
                error, truncated = "page_extraction_failed", True
                break
            if text.strip():
                page = (f"\n[Page {index + 1}]\n" + text).replace("\0", " ").encode("utf8", errors="replace")
                remaining = MAX_TEXT_BYTES - size
                if len(page) > remaining:
                    parts.append(page[:remaining].decode("utf8", errors="ignore"))
                    truncated = True
                    break
                parts.append(page.decode("utf8"))
                size += len(page)
            read += 1
    except TimeLimit:
        error, truncated = "time_limit", True
    text = "".join(parts)
    return {"status": "partial" if truncated and text else "ready" if text else "unavailable",
            "text": text, "truncated": truncated, "pages_read": read, "total_pages": total,
            "error": error or (None if text else "no_text")}


def limit_memory(resource):
    try:
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    except (ValueError, OSError):
        # macOS refuses every memory limit. There pypdf's per-stream decoding caps, the page and text limits, the CPU
        # limit and the parent's timeout bound the work, rather than every extraction failing. Elsewhere it stays fatal.
        if sys.platform != "darwin":
            raise


def main():
    try:
        import resource

        limit_memory(resource)
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except ImportError:
        pass
    logging.disable(logging.CRITICAL)
    deadline = arm()
    body = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    try:
        result = extract(body, deadline) if len(body) <= 8 * 1024 * 1024 else {"status": "failed", "error": "input_limit", "text": ""}
    except TimeLimit:
        # The limit came before any page was read, while the document itself was being parsed.
        result = {"status": "failed", "error": "extraction_timeout", "text": ""}
    except Exception:
        result = {"status": "failed", "error": "extraction_failed", "text": ""}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()

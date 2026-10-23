"""Bounded subprocess entry point for PDF text extraction."""

import io
import json
import logging
import sys

MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_PAGES = 200


def extract(body):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body), strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        return {"status": "unavailable", "error": "password_required", "text": "", "pages_read": 0}
    total = len(reader.pages)
    parts, size, read = [], 0, 0
    truncated = total > MAX_PAGES
    error = None
    for index in range(min(total, MAX_PAGES)):
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
    text = "".join(parts)
    return {"status": "partial" if truncated and text else "ready" if text else "unavailable",
            "text": text, "truncated": truncated, "pages_read": read, "total_pages": total,
            "error": error or (None if text else "no_text")}


def main():
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (8, 9))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except ImportError:
        pass
    logging.disable(logging.CRITICAL)
    body = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    try:
        result = extract(body) if len(body) <= 8 * 1024 * 1024 else {"status": "failed", "error": "input_limit", "text": ""}
    except Exception:
        result = {"status": "failed", "error": "extraction_failed", "text": ""}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()

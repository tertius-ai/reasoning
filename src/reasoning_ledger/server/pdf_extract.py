"""PDF extraction outside database transactions and the API process."""

import json
import subprocess
import sys
import threading

from .pdf_extract_worker import MAX_TEXT_BYTES

_slots = threading.BoundedSemaphore(2)


def extract_pdf(body):
    if not _slots.acquire(timeout=1):
        return {"status": "failed", "error": "extractor_busy", "text": ""}
    try:
        result = subprocess.run([sys.executable, "-I", "-B", "-m", "reasoning_ledger.server.pdf_extract_worker"], input=body,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=12, check=False)
        if result.returncode or len(result.stdout) > 6 * MAX_TEXT_BYTES + 4096:
            return {"status": "failed", "error": "resource_limit", "text": ""}
        value = json.loads(result.stdout)
        text = value.get("text")
        if (not isinstance(text, str) or len(text.encode("utf8")) > MAX_TEXT_BYTES
                or value.get("status") not in {"ready", "partial", "unavailable", "failed"}):
            raise ValueError("invalid extractor result")
        return value
    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": "extraction_timeout", "text": ""}
    except (OSError, ValueError, UnicodeError, TypeError, AttributeError):
        return {"status": "failed", "error": "extraction_failed", "text": ""}
    finally:
        _slots.release()

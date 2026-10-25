"""Delivery of an account's camt.053: HTTPS POST to the configured target, or a local folder.

HTTPS: ``POST <url>`` with ``Authorization: Bearer <token from the environment>`` and the
document as ``application/xml`` body. Network errors and HTTP 5xx are retried with a
doubling backoff, any other answer is final; only a 2xx counts as delivered. Redirects are
not followed, so the token goes nowhere but the configured URL. Targets deduplicate, so
re-delivering an overlapping period is fine.

Folder: one file per account and period, written atomically; a re-run of the same period
replaces the file.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx

from . import __version__
from .config import DeliveryConfig, TargetConfig, mask_iban, read_token
from .errors import DeliveryError
from .fetch import FetchResult

log = logging.getLogger(__name__)

FILE_MODE = 0o640


def deliver(
    result: FetchResult,
    target: TargetConfig,
    delivery: DeliveryConfig,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Deliver ``result`` to ``target``; returns a short description of where it went (for
    the log: the folder, not the file name, which contains the full IBAN)."""
    if target.folder is not None:
        write_to_folder(result, target.folder)
        return f"folder {target.folder}"
    if target.url is None:
        raise DeliveryError("the target has neither url nor folder")
    token = read_token(target, environ)
    status = post(target.url, result.document, token, delivery, transport=transport, sleep=sleep)
    return f"HTTP {status}"


def post(
    url: str,
    document: bytes,
    token: str,
    delivery: DeliveryConfig,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """POST the document; returns the 2xx status or raises :class:`DeliveryError`."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/xml",
        "User-Agent": f"fints2camt/{__version__}",
    }
    delay = float(delivery.backoff_seconds)
    with httpx.Client(transport=transport, timeout=delivery.timeout_seconds, follow_redirects=False) as client:
        for attempt in range(1, delivery.attempts + 1):
            try:
                response = client.post(url, content=document, headers=headers)
            except httpx.HTTPError as exc:
                error = DeliveryError(f"network error: {type(exc).__name__}")
            else:
                if 200 <= response.status_code < 300:
                    return response.status_code
                if response.status_code < 500:
                    raise DeliveryError(
                        f"the target answered HTTP {response.status_code}; not retrying", response.status_code
                    )
                error = DeliveryError(f"the target answered HTTP {response.status_code}", response.status_code)
            if attempt < delivery.attempts:
                log.warning("delivery attempt %d/%d failed (%s); retrying in %gs", attempt, delivery.attempts, error, delay)
                sleep(delay)
                delay *= 2
    raise DeliveryError(f"{error} after {delivery.attempts} attempt(s)", error.status)


def statement_file_name(result: FetchResult) -> str:
    return f"{result.iban}_{result.window.start.isoformat()}_{result.window.end.isoformat()}.camt053.xml"


def write_to_folder(result: FetchResult, folder: Path) -> Path:
    """Write the document atomically (temporary file + rename) into ``folder``."""
    path = folder / statement_file_name(result)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=folder)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(result.document)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, FILE_MODE)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise DeliveryError(
            f"cannot write the statement of {mask_iban(result.iban)} into {folder}: {exc.strerror}"
        ) from None
    return path

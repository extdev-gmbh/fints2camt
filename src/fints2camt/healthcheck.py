"""The optional health-check ping after a run (e.g. an Uptime Kuma or healthchecks.io push URL).

A plain GET of the configured URL, nothing is sent along. Push URLs usually carry a secret,
so the URL is never logged. A failing ping is a warning; it never changes the run's result.
"""

from __future__ import annotations

import logging

import httpx

from . import __version__
from .config import HealthcheckConfig

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10


def ping(healthcheck: HealthcheckConfig, success: bool, transport: httpx.BaseTransport | None = None) -> None:
    url = healthcheck.success_url if success else healthcheck.failure_url
    if url is None:
        return
    what = "success" if success else "failure"
    try:
        with httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = client.get(url, headers={"User-Agent": f"fints2camt/{__version__}"})
    except httpx.HTTPError as exc:
        log.warning("the %s health-check ping failed: %s", what, type(exc).__name__)
        return
    if not response.is_success:
        log.warning("the %s health-check ping answered HTTP %d", what, response.status_code)

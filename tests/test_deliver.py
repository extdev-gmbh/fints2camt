import datetime
import stat

import httpx
import pytest

from fints2camt.config import DeliveryConfig, HealthcheckConfig, TargetConfig
from fints2camt.deliver import deliver
from fints2camt.errors import ConfigError, DeliveryError
from fints2camt.fetch import FetchResult
from fints2camt.healthcheck import ping
from fints2camt.model import FetchWindow

from conftest import OWN_IBAN

URL = "https://accounting.example.invalid/camt"
TARGET = TargetConfig(url=URL, token_env="TARGET_TOKEN")
ENV = {"TARGET_TOKEN": "synthetic-token"}
DELIVERY = DeliveryConfig(attempts=3, backoff_seconds=5)
RESULT = FetchResult(
    iban=OWN_IBAN,
    window=FetchWindow(datetime.date(2026, 10, 21), datetime.date(2026, 10, 25)),
    document=b"<Document/>",
    entry_count=0,
    source_format="mt940",
)


class Target:
    """An HTTP target answering with the given statuses (or raising), recording requests."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, headers={"Location": "https://elsewhere.invalid/"} if 300 <= answer < 400 else {})


def send(target: Target, delivery: DeliveryConfig = DELIVERY) -> tuple[str, list[float]]:
    sleeps: list[float] = []
    where = deliver(RESULT, TARGET, delivery, environ=ENV, transport=httpx.MockTransport(target), sleep=sleeps.append)
    return where, sleeps


def test_post_with_bearer_token_and_xml_body() -> None:
    target = Target(201)
    assert send(target) == ("HTTP 201", [])
    (request,) = target.requests
    assert request.method == "POST" and str(request.url) == URL
    assert request.headers["Authorization"] == "Bearer synthetic-token"
    assert request.headers["Content-Type"] == "application/xml"
    assert request.content == b"<Document/>"


@pytest.mark.parametrize("status", [400, 401, 404, 409, 422])
def test_4xx_is_not_retried(status: int) -> None:
    target = Target(status)
    with pytest.raises(DeliveryError) as excinfo:
        send(target)
    assert excinfo.value.status == status
    assert len(target.requests) == 1
    assert "synthetic-token" not in str(excinfo.value)


def test_redirect_is_not_followed() -> None:
    target = Target(302)
    with pytest.raises(DeliveryError):
        send(target)
    assert [str(r.url) for r in target.requests] == [URL]


def test_5xx_and_network_errors_are_retried_with_backoff() -> None:
    target = Target(503, httpx.ConnectError("refused"), 200)
    assert send(target) == ("HTTP 200", [5, 10])
    assert len(target.requests) == 3


def test_persistent_5xx_fails_after_the_configured_attempts() -> None:
    target = Target(500, 502, 503)
    with pytest.raises(DeliveryError, match="HTTP 503 after 3 attempt") as excinfo:
        send(target)
    assert excinfo.value.status == 503


def test_persistent_network_error() -> None:
    target = Target(*[httpx.ReadTimeout("slow")] * 3)
    with pytest.raises(DeliveryError, match="ReadTimeout") as excinfo:
        send(target)
    assert excinfo.value.status is None


def test_missing_token_is_a_configuration_error() -> None:
    with pytest.raises(ConfigError, match="TARGET_TOKEN"):
        deliver(RESULT, TARGET, DELIVERY, environ={}, transport=httpx.MockTransport(Target()))


def test_folder_target_writes_one_file_per_account_and_period(tmp_path) -> None:
    folder = tmp_path / "out" / "camt"
    where = deliver(RESULT, TargetConfig(folder=folder), DELIVERY)
    path = folder / f"{OWN_IBAN}_2026-10-21_2026-10-25.camt053.xml"
    assert where == f"folder {folder}"
    assert path.read_bytes() == b"<Document/>"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    # the same period again replaces the file, nothing else is left behind
    deliver(RESULT, TargetConfig(folder=folder), DELIVERY)
    assert [p.name for p in folder.iterdir()] == [path.name]


def test_unwritable_folder(tmp_path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(DeliveryError, match="cannot write") as excinfo:
        deliver(RESULT, TargetConfig(folder=blocker / "camt"), DELIVERY)
    assert "DE89…3000" in str(excinfo.value)
    assert OWN_IBAN not in str(excinfo.value)


def test_health_check_pings_the_matching_url_and_never_raises() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if "down" in str(request.url):
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200)

    config = HealthcheckConfig(success_url="https://kuma.invalid/push/s3cret?status=up",
                               failure_url="https://kuma.invalid/push/s3cret?status=down")
    transport = httpx.MockTransport(handler)
    ping(config, True, transport)
    ping(config, False, transport)  # a failing ping is only logged
    ping(HealthcheckConfig(), True, transport)
    assert urls == [config.success_url, config.failure_url]
    assert "s3cret" not in repr(config)

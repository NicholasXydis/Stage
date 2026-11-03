import socket
import ssl

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from stage.http import (
    BreakerOpenError,
    HostBudgetExceededError,
    HttpStatusError,
    failure_text,
    returned,
    skipped_by_breaker,
)
from stage.http.client import TRIPPED, _span, transport_text
from stage.sources import PayloadValidationError, sentences
from stage.sources.base import validation_problem

REQUEST = httpx.Request("GET", "https://boards-api.greenhouse.io/v1/boards/acme/jobs")


def _caused(error: httpx.HTTPError, cause: BaseException) -> httpx.HTTPError:
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (httpx.ReadTimeout("", request=REQUEST), "took too long to answer"),
        (httpx.ConnectTimeout("", request=REQUEST), "did not accept a connection in time"),
        (httpx.PoolTimeout("", request=REQUEST), "timed out"),
        (httpx.ConnectError("[Errno 8] nodename nor servname", request=REQUEST), "look up"),
        (httpx.ConnectError("[Errno -2] Name or service not known", request=REQUEST), "look up"),
        (httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=REQUEST), "look up"),
        (
            _caused(httpx.ConnectError("", request=REQUEST), socket.gaierror(8, "lookup")),
            "network may be down",
        ),
        (
            _caused(
                httpx.ConnectError("", request=REQUEST),
                ssl.SSLCertVerificationError("certificate verify failed"),
            ),
            "Wi-Fi login page",
        ),
        (
            _caused(httpx.ConnectError("", request=REQUEST), ConnectionRefusedError(61, "no")),
            "refused the connection",
        ),
        (httpx.ConnectError("down", request=REQUEST), "could not connect"),
        (httpx.ProxyError("403", request=REQUEST), "proxy refused"),
        (httpx.RemoteProtocolError("closed", request=REQUEST), "broke off its answer"),
        (httpx.ReadError("reset", request=REQUEST), "connection to"),
        (httpx.WriteError("reset", request=REQUEST), "connection to"),
        (httpx.UnsupportedProtocol("ftp", request=REQUEST), "failed"),
    ],
)
def test_every_transport_failure_names_the_host_in_plain_words(
    error: httpx.HTTPError, said: str
) -> None:
    text = failure_text(error)
    assert said in text
    assert "boards-api.greenhouse.io" in text
    assert "Error" not in text and "Timeout" not in text and ";" not in text


def test_a_transport_failure_without_a_request_still_reads_as_a_sentence() -> None:
    assert transport_text(httpx.ReadTimeout("")) == "the site took too long to answer"


@pytest.mark.parametrize(
    ("status", "said"),
    [(403, "403 (forbidden)"), (404, "404 (not found)"), (429, "429 (too many requests)")],
)
def test_a_status_names_the_host_and_says_what_the_number_means(status: int, said: str) -> None:
    assert returned("api.lever.co", status) == f"api.lever.co returned {said}"


def test_an_unusual_status_is_given_without_a_guess() -> None:
    assert returned("api.lever.co", 418) == "api.lever.co returned 418"


def test_a_status_error_names_the_host_that_answered_not_the_shared_bucket() -> None:
    request = httpx.Request("POST", "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/x/jobs")
    error = HttpStatusError("workday", httpx.Response(422, request=request))
    assert str(error) == "acme.wd5.myworkdayjobs.com returned 422 (request rejected)"
    assert HttpStatusError("workday", httpx.Response(500)).status == 500
    assert str(HttpStatusError("workday", httpx.Response(500))).startswith("workday returned")


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (PayloadValidationError("custom_json/acme has no <item> elements"), "has no <item>"),
        (ValueError("a bad slug"), "a bad slug"),
        (KeyError("title"), "Stage hit an unexpected problem ('title')"),
        (TypeError("no"), "Stage hit an unexpected problem (no)"),
        (RuntimeError(""), "Stage could not tell what went wrong"),
    ],
)
def test_other_failures_lose_their_class_name(error: Exception, said: str) -> None:
    assert failure_text(error) == said or said in failure_text(error)
    assert type(error).__name__ not in failure_text(error)


def test_the_doctor_can_tell_a_breaker_skip_in_old_and_new_wording() -> None:
    old = "BreakerOpenError: workday circuit breaker is open after 6 consecutive failures"
    new = str(BreakerOpenError("Stage's circuit breaker paused workday after 6 failures in a row"))
    assert skipped_by_breaker(old) and skipped_by_breaker(new) and skipped_by_breaker(TRIPPED)
    assert not skipped_by_breaker("api.lever.co returned 404 (not found)")
    assert not skipped_by_breaker(str(HostBudgetExceededError("ceiling")))


@pytest.mark.parametrize(
    ("seconds", "said"),
    [
        (1, "1 second"),
        (45, "45 seconds"),
        (600, "10 minutes"),
        (1800, "30 minutes"),
        (3600, "1 hour"),
        (5400, "1.5 hours"),
        (86400, "24 hours"),
    ],
)
def test_a_wait_reads_in_the_largest_whole_unit(seconds: float, said: str) -> None:
    assert _span(seconds) == said


def test_notes_join_as_sentences_without_semicolons() -> None:
    assert sentences("", "stopped at the cap", "A later page answered 304.") == (
        "Stopped at the cap. A later page answered 304."
    )
    assert sentences("", " ") == ""


class _Board(BaseModel):
    jobs: list[int]


def test_a_validation_problem_names_the_field() -> None:
    with pytest.raises(ValidationError) as missing:
        _Board.model_validate({})
    with pytest.raises(ValidationError) as wrong:
        _Board.model_validate({"jobs": "many"})
    assert validation_problem(missing.value) == 'no "jobs" field'
    assert validation_problem(wrong.value).startswith('an invalid "jobs" field (')

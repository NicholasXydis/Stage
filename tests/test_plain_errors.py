import pytest

from stage.domain.text import did_you_mean, plain_error


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("HttpStatusError: api.ashbyhq.com returned 404", "api.ashbyhq.com returned 404"),
        ("ReadTimeout: ", "the answer timed out"),
        ("ConnectError: [Errno 8] nodename", "could not connect: [Errno 8] nodename"),
        (
            "BreakerOpenError: workday circuit breaker is open after 6 consecutive failures",
            "workday circuit breaker is open after 6 consecutive failures",
        ),
        (
            "PayloadValidationError: speedyapply: every file failed: a: BucketBlockedError",
            "speedyapply: every file failed: a",
        ),
        ("CancelledError", "the request was cancelled"),
        ("", "Stage could not tell what went wrong"),
        ("Error: the plain word stays", "Error: the plain word stays"),
        ("the board took too long", "the board took too long"),
    ],
)
def test_a_stored_error_is_shown_without_python_class_names(stored: str, shown: str) -> None:
    assert plain_error(stored) == shown


def test_did_you_mean_is_worded_one_way() -> None:
    assert did_you_mean("canada") == 'Did you mean "canada"?'

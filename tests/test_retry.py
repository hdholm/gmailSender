"""
Retry behaviour - the core of issue #1.

The bug report was an HTTP 500 "Temporary System Problem." (reason
backendError) that killed a delivery outright.  These tests pin down which
failures are retried, which are not, and how long we are willing to wait.
"""

import socket
import ssl

import support                                              # noqa: F401
import gmail_insert as g
import httplib2
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError

BACKEND_ERROR = "Temporary System Problem."
NO_SLEEP = lambda seconds: None                             # noqa: E731


def scripted(failures, exc_factory=None, result=None):
    """A fake insert_message that fails `failures` times, then succeeds."""
    exc_factory = exc_factory or (lambda: HttpError(500, BACKEND_ERROR))
    state = {"calls": 0}

    def insert(service, raw):
        state["calls"] += 1
        if state["calls"] <= failures:
            raise exc_factory()
        return result or {"id": "OK", "threadId": "T"}

    return insert, state


def test_issue_1_backend_error_is_retried_and_recovers():
    """The exact failure from the bug report must not lose the message."""
    g.insert_message, state = scripted(2)
    result = g.insert_with_retry(None, b"raw", max_attempts=5,
                                 initial_backoff=0.001, sleep=NO_SLEEP)
    support.expect(result["id"] == "OK", "expected eventual success")
    support.expect(state["calls"] == 3, f"expected 3 attempts, got {state['calls']}")


def test_retryable_statuses():
    for status in (408, 429, 500, 502, 503, 504):
        support.expect(g.is_retryable(HttpError(status, "transient")),
                       f"HTTP {status} should be retryable")


def test_permanent_statuses_are_not_retried():
    for status in (400, 401, 403, 404, 412):
        support.expect(not g.is_retryable(HttpError(status, "permanent")),
                       f"HTTP {status} must not be retried")

    g.insert_message, state = scripted(99, lambda: HttpError(400, "Bad Request"))
    try:
        g.insert_with_retry(None, b"raw", max_attempts=5,
                            initial_backoff=0.001, sleep=NO_SLEEP)
    except HttpError:
        pass
    else:
        raise AssertionError("a 400 should propagate")
    support.expect(state["calls"] == 1,
                   f"a 400 must be tried once, not {state['calls']} times")


def test_transport_errors_are_retried():
    """Non-HttpError network failures used to escape as tracebacks."""
    transient = [
        ConnectionResetError("connection reset by peer"),
        TimeoutError("timed out"),
        socket.gaierror("name resolution failed"),
        ssl.SSLError("handshake failure"),
        BrokenPipeError("broken pipe"),
        httplib2.ServerNotFoundError("unable to find server"),
        TransportError("transport failed"),
    ]
    for exc in transient:
        support.expect(g.is_retryable(exc),
                       f"{type(exc).__name__} should be retryable")


def test_unexpected_exceptions_are_not_retried():
    for exc in (ValueError("bad value"), KeyError("missing")):
        support.expect(not g.is_retryable(exc),
                       f"{type(exc).__name__} must not be retried")


def test_gives_up_after_max_attempts():
    g.insert_message, state = scripted(99)
    try:
        g.insert_with_retry(None, b"raw", max_attempts=4,
                            initial_backoff=0.001, sleep=NO_SLEEP)
    except HttpError:
        pass
    else:
        raise AssertionError("expected the last error to propagate")
    support.expect(state["calls"] == 4,
                   f"expected exactly 4 attempts, got {state['calls']}")


def test_no_sleep_after_the_final_attempt():
    """Never burn backoff time we are not going to use."""
    slept = []
    g.insert_message, _ = scripted(99)
    try:
        g.insert_with_retry(None, b"raw", max_attempts=3,
                            initial_backoff=0.001, sleep=slept.append)
    except HttpError:
        pass
    support.expect(len(slept) == 2,
                   f"3 attempts should mean 2 sleeps, got {len(slept)}")


def test_backoff_grows_and_stays_jittered():
    """Equal jitter: each delay lands in [backoff/2, backoff], capped."""
    slept = []
    g.insert_message, _ = scripted(99)
    try:
        g.insert_with_retry(None, b"raw", max_attempts=6, initial_backoff=1.0,
                            max_backoff=8.0, deadline=10_000, sleep=slept.append)
    except HttpError:
        pass
    for i, delay in enumerate(slept):
        backoff = min(8.0, 1.0 * (2 ** i))
        support.expect(backoff / 2 <= delay <= backoff,
                       f"delay {delay} outside [{backoff / 2}, {backoff}]")
    support.expect(max(slept) <= 8.0, "max_backoff not respected")
    support.expect(len(set(slept)) > 1, "delays look unjittered")


def test_retry_after_header_is_honoured():
    g.insert_message, _ = scripted(
        99, lambda: HttpError(429, "Rate limited", {"retry-after": "9"}))
    slept = []
    try:
        g.insert_with_retry(None, b"raw", max_attempts=2, initial_backoff=0.001,
                            max_backoff=30, deadline=1000, sleep=slept.append)
    except HttpError:
        pass
    support.expect(abs(slept[0] - 9.0) < 1e-6,
                   f"expected a 9s wait from Retry-After, got {slept[0]}")


def test_retry_after_is_capped_by_max_backoff():
    """A hostile or absurd Retry-After must not stall procmail for an hour."""
    g.insert_message, _ = scripted(
        99, lambda: HttpError(503, "Unavailable", {"retry-after": "3600"}))
    slept = []
    try:
        g.insert_with_retry(None, b"raw", max_attempts=2, initial_backoff=0.001,
                            max_backoff=5, deadline=1000, sleep=slept.append)
    except HttpError:
        pass
    support.expect(slept[0] <= 5, f"Retry-After should cap at 5s, got {slept[0]}")


def test_malformed_retry_after_falls_back_to_backoff():
    """HTTP-date form, or junk, must not crash the retry loop."""
    for value in ("Wed, 21 Oct 2026 07:28:00 GMT", "soon", ""):
        g.insert_message, _ = scripted(
            99, lambda v=value: HttpError(503, "Unavailable", {"retry-after": v}))
        slept = []
        try:
            g.insert_with_retry(None, b"raw", max_attempts=2, initial_backoff=2.0,
                                max_backoff=4, deadline=1000, sleep=slept.append)
        except HttpError:
            pass
        support.expect(1.0 <= slept[0] <= 2.0,
                       f"expected normal backoff for Retry-After={value!r}, got {slept[0]}")


def test_deadline_bounds_total_wait():
    """procmail holds an MTA connection open; the wait has to be bounded."""
    clock = {"now": 0.0}
    g.insert_message, state = scripted(99)
    try:
        g.insert_with_retry(None, b"raw", max_attempts=1000, initial_backoff=1.0,
                            max_backoff=30, deadline=5.0,
                            sleep=lambda s: clock.__setitem__("now", clock["now"] + s),
                            monotonic=lambda: clock["now"])
    except HttpError:
        pass
    else:
        raise AssertionError("expected the deadline to end the loop")
    support.expect(clock["now"] <= 5.0,
                   f"slept {clock['now']}s against a 5s deadline")
    support.expect(state["calls"] < 1000, "deadline did not stop the attempts")


def test_http_status_and_short_helpers():
    exc = HttpError(500, BACKEND_ERROR)
    support.expect(g.http_status(exc) == 500, "status extraction failed")
    support.expect(g.http_status(ValueError("x")) is None,
                   "non-HTTP errors have no status")
    summary = g._short(exc)
    support.expect("HTTP 500" in summary and "\n" not in summary,
                   f"unhelpful summary: {summary}")

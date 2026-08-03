#!/usr/bin/env python3
"""
Subprocess entry point for the CLI tests.

Runs gmail_insert.main() with insert_message() replaced by a scripted fake,
so exit codes, spool contents and per-user credential use can be observed
exactly as procmail and cron would see them.  Never touches the network.

Environment:
    GMAIL_TEST_MODE   ok            every insert succeeds
                      always_fail   every insert raises HTTP 500 (issue #1)
                      flaky:N       the first N attempts fail, then success
                      http:CODE     every insert raises that HTTP status
    GMAIL_TEST_AUDIT  optional path; one JSON line per successful insert,
                      recording the token file the service was built from,
                      the subject, and the attempt number
"""

import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "stubs"))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

import gmail_insert as g                                    # noqa: E402
from googleapiclient.errors import HttpError                # noqa: E402

MODE = os.environ.get("GMAIL_TEST_MODE", "ok")
AUDIT = os.environ.get("GMAIL_TEST_AUDIT") or None
BACKEND_ERROR = "Temporary System Problem."                 # verbatim from issue #1

_attempts = {"n": 0}


def _record(service, raw, attempt):
    if not AUDIT:
        return
    entry = {
        "token": os.path.basename(getattr(service, "token_file", None) or "?"),
        "subject": g.describe(raw)["subject"],
        "attempt": attempt,
    }
    with open(AUDIT, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def fake_insert(service, raw: bytes) -> dict:
    _attempts["n"] += 1
    attempt = _attempts["n"]

    if MODE == "always_fail":
        raise HttpError(500, BACKEND_ERROR)
    if MODE.startswith("http:"):
        raise HttpError(int(MODE.split(":", 1)[1]), "Scripted failure")
    if MODE.startswith("flaky:") and attempt <= int(MODE.split(":", 1)[1]):
        raise HttpError(500, BACKEND_ERROR)

    # Invariant worth asserting on every delivery: the mbox envelope line
    # must never reach the API, and the payload must be valid base64url.
    payload = base64.urlsafe_b64decode(base64.urlsafe_b64encode(g.strip_envelope(raw)).decode("utf-8"))
    assert not payload.startswith(b"From "), "mbox envelope leaked into API payload"

    _record(service, raw, attempt)
    return {
        "id": f"TESTID{attempt}",
        "threadId": f"TESTTHREAD{attempt}",
        "labelIds": ["INBOX", "UNREAD"],
    }


g.insert_message = fake_insert

if __name__ == "__main__":
    sys.exit(g.main())

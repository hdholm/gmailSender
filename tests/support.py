"""
Shared helpers for the gmail_insert regression suite.

Importing this module puts tests/stubs/ and the repository root at the front
of sys.path, so `import gmail_insert` works and picks up the fake Google
client libraries instead of the real ones.  Import it before gmail_insert.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
STUBS_DIR = TESTS_DIR / "stubs"
DRIVER = TESTS_DIR / "driver.py"

# Stubs first: they must win even when the real client libraries are
# installed, so the suite behaves identically everywhere.
for _entry in (str(STUBS_DIR), str(REPO_ROOT)):
    if _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

SUBPROCESS_TIMEOUT = 60          # a hang is a failure, not a stalled suite

# Keep gmail_insert's diagnostics out of the runner's output for in-process
# tests.  Subprocess tests read the child's stderr directly, so assertions
# about logging are unaffected.
logging.getLogger("gmail_insert").setLevel(logging.CRITICAL)


def child_env(mode="ok", audit=None, **extra):
    """Environment for a driver subprocess (see tests/driver.py)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(STUBS_DIR), str(REPO_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    env["GMAIL_TEST_MODE"] = mode
    env["GMAIL_TEST_AUDIT"] = str(audit) if audit else ""
    env.update({k: str(v) for k, v in extra.items()})
    return env


def run_cli(args, *, mode="ok", stdin=b"", audit=None, timeout=SUBPROCESS_TIMEOUT):
    """
    Run gmail_insert's main() in a subprocess with a scripted fake API.

    Subprocess rather than in-process so that exit codes - the whole point of
    the procmail contract - are observed exactly as procmail would see them.
    """
    return subprocess.run(
        [sys.executable, str(DRIVER), *[str(a) for a in args]],
        input=stdin,
        capture_output=True,
        env=child_env(mode=mode, audit=audit),
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )


def message(subject="Test message", sender="sender@example.com", body="Hello.\n",
            envelope=True):
    """An mbox message, with or without the leading 'From ' envelope line."""
    head = f"From {sender} Tue May 12 06:01:37 2026\n" if envelope else ""
    return (
        f"{head}"
        f"From: Sender <{sender}>\n"
        f"To: you@gmail.com\n"
        f"Subject: {subject}\n"
        f"Date: Tue, 12 May 2026 06:01:37 +0000\n"
        f"Content-Type: text/plain\n"
        f"\n{body}"
    ).encode()


def temp_spool():
    """A private spool directory; every test gets its own."""
    return tempfile.mkdtemp(prefix="gmail_spool_")


def token_dir(*user_ids):
    """A token directory pre-populated with token_<ID>.json for each user."""
    path = Path(tempfile.mkdtemp(prefix="gmail_tokens_"))
    for user_id in user_ids:
        (path / f"token_{user_id}.json").write_text("{}")
    return path


def audit_file():
    return Path(tempfile.mkdtemp(prefix="gmail_audit_")) / "audit.jsonl"


def read_audit(path):
    """Deliveries recorded by the driver, in order: {token, subject, attempt}."""
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def spool_files(spool_dir):
    return sorted(Path(spool_dir).glob("*.mbx"))


def spool_names(spool_dir):
    return [p.name for p in spool_files(spool_dir)]


def json_output(completed):
    """Parse the --json payload from a run_cli() result."""
    return json.loads(completed.stdout.decode() or "[]")


def expect(condition, message_text):
    if not condition:
        raise AssertionError(message_text)

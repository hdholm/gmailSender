#!/usr/bin/env python3
"""
Gmail API - mbox stdin → Gmail Insert
======================================
Reads one or more messages in mbox format from standard input and inserts
each one directly into the authenticated user's Gmail mailbox using the
Gmail API's messages.insert method (no SMTP, no sending).

Because this script is normally run as a procmail delivery agent, it is
written so that a message is *never* dropped on the floor:

  1. Transient API failures (HTTP 429/500/502/503/504, socket timeouts,
     TLS/connection resets) are retried automatically with exponential
     backoff plus jitter, honouring any Retry-After header.
  2. If the message still cannot be handed to Gmail, the original bytes are
     written to an on-disk spool directory with an atomic
     write-fsync-rename, and the script exits 0 so procmail considers the
     delivery complete.  The spool file name carries the destination
     --user, so a later `--flush` run (cron) can drain every account with
     that account's own saved credentials.
  3. If even spooling fails, the script exits EX_TEMPFAIL (75) so procmail
     falls through to the next recipe and/or the MTA requeues the message.

Usage:
    python3 gmail_insert.py < single_message.mbox
    python3 gmail-insert.py --mbox < multiple_message.mbox
    cat archive.mbox | python3 gmail_insert.py
    python3 gmail_insert.py --flush          # drain the spool, all users
    python3 gmail_insert.py --flush --user 2 # drain user 2's spool only
    python3 gmail_insert.py --auth --user 2  # one-time interactive OAuth

Options:
    --debug           Produce debugging output
    --user ID         Integer identifier of credentials file; with --flush,
                      limits the run to that one account
    --auth            Run the interactive OAuth flow and exit
    --flush           Re-attempt delivery of spooled messages and exit
    --mbox            Take potentially multiple mbox formatted messages
    --spool-dir DIR   Where undeliverable messages are parked
    --on-failure      spool (default) or tempfail
    (see --help for the retry tuning knobs)

mbox format:
    Each message starts with a "From " (From-space) separator line, e.g.:
        From alice@example.com Mon Jan  1 00:00:00 2024

    Single messages without the separator line are also accepted.

Requirements:
    pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client

Setup:
    1. Enable Gmail API at https://console.cloud.google.com/
    2. Create OAuth 2.0 credentials (Desktop App) and download as credentials.json
    3. Place credentials.json in the same directory as this script
    4. Run `gmail_insert.py --auth` once — a browser window opens for auth

"""

import argparse
import base64
import email
import email.policy
import errno
import json
import logging
import os
import random
import re
import socket
import ssl
import sys
import time
import uuid

from email import message_from_bytes
from email.message import EmailMessage
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/gmail.insert"]
CREDENTIALS_FILE = "credentials.json" # OAuth2 client secret downloaded from GCP

SCRIPT_DIR = Path(__file__).resolve().parent

# sysexits.h - procmail/sendmail understand this one as "try again later"
EX_TEMPFAIL = 75

# HTTP statuses that are worth another attempt.  429 = rate limited,
# 5xx = Google-side hiccup (issue #1: 500 "Temporary System Problem.").
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_INITIAL_BACKOFF = 1.0     # seconds
DEFAULT_MAX_BACKOFF = 30.0        # seconds
DEFAULT_DEADLINE = 120.0          # total seconds spent retrying one message
DEFAULT_SPOOL = "~/.gmail_insert/spool"
DEFAULT_USER = 0                  # --user when none is given

log = logging.getLogger(__name__)


def _transient_exception_types() -> tuple:
    """Network-layer errors that justify a retry, resolved defensively."""
    types = [
        TimeoutError,
        ConnectionError,          # includes ConnectionReset/Aborted/Refused
        ConnectionResetError,
        BrokenPipeError,
        socket.timeout,
        socket.gaierror,
        ssl.SSLError,
    ]
    try:                                     # pulled in by the API client
        import httplib2
        types.append(httplib2.HttpLib2Error)
    except Exception:                        # pragma: no cover - optional dep
        pass
    try:
        from google.auth.exceptions import TransportError
        types.append(TransportError)
    except Exception:                        # pragma: no cover - optional dep
        pass
    return tuple(dict.fromkeys(types))


TRANSIENT_EXCEPTIONS = _transient_exception_types()


class AuthUnavailable(Exception):
    """Raised when we cannot authenticate without human interaction."""


class SpoolError(Exception):
    """Raised when a message could not be parked on disk."""


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _locate(filename: str, override: str | None = None) -> Path:
    """
    Resolve a credential/token file.

    Under procmail the working directory is $MAILDIR, not the script
    directory, so a bare relative name silently fails to resolve and every
    message ends up spooled.  Look in the CWD first (previous behaviour),
    then next to the script.
    """
    if override:
        return Path(override).expanduser()
    here = Path(filename)
    if here.exists():
        return here.resolve()
    return SCRIPT_DIR / filename


def get_gmail_service(cred_id: str, *, interactive: bool = False,
                      credentials_file: str | None = None,
                      token_dir: str | None = None):
    """
    Authenticate and return an authorised Gmail API service object.

    `interactive` must be True before a browser-based consent flow is
    attempted.  When running as an MDA it stays False: blocking on
    run_local_server() would hang procmail forever while holding the
    message hostage.
    """
    creds = None
    token_file = (Path(token_dir).expanduser() / f"token_{cred_id}.json"
                  if token_dir else _locate(f"token_{cred_id}.json"))
    cred_file = _locate(CREDENTIALS_FILE, credentials_file)

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise AuthUnavailable(
                    f"token refresh failed ({exc}); re-run with --auth"
                ) from exc
        elif interactive:
            if not cred_file.exists():
                raise AuthUnavailable(
                    f"'{CREDENTIALS_FILE}' not found at {cred_file}. "
                    "Download OAuth 2.0 credentials from the Google Cloud "
                    "Console and save them there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                    str(cred_file), SCOPES
                    )
            creds = flow.run_local_server(port=0)
        else:
            raise AuthUnavailable(
                f"no usable token at {token_file} and this is a "
                "non-interactive run; authorise once with "
                f"'gmail_insert.py --auth --user {cred_id}'"
            )

        try:
            token_file.parent.mkdir(parents=True, exist_ok=True)
            with open(token_file, "w") as fh:
                fh.write(creds.to_json())
            os.chmod(token_file, 0o600)
            log.info("[AUTH] Token saved to %s", token_file)
        except OSError as exc:
            # Not fatal: the in-memory credentials still work for this run.
            log.warning("[WARN] Could not write %s: %s", token_file, exc)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# mbox parsing
# ---------------------------------------------------------------------------

def split_mbox(raw: bytes) -> list[bytes]:
    """
    Split raw mbox bytes into per-message chunks.

    Handles two cases (only invoked when --mbox given):
      1. Proper mbox with one or more "From " separator lines.
      2. A bare RFC-2822 message with no "From " line (single message).
    """
    if raw.startswith(b"From "):
        messages = []
        m_start = raw.rfind(b'\nFrom ')
        m_end = len(raw) - 1
        while m_start != -1:
            log.debug("[INFO] Found message at %d - %d: %s", m_start, m_end,
                      raw[m_start+1:m_start+20])
            messages.append(raw[m_start+1:m_end])
            m_end = m_start
            m_start = raw.rfind(b'\nFrom ', 0 , m_end)
        messages.append(raw[:m_end])
        log.debug("[INFO] Found message at %d - %d: %s", m_start, m_end,
                  raw[:20])
        return messages
    else:
        return [raw]


def parse_headers(raw: bytes) -> EmailMessage:
    """
    Parse a message for reporting purposes.

    Uses email.policy.default so the result is a modern EmailMessage rather
    than the legacy Message class.  UTF-8 bodies and headers are handled
    correctly without any monkey-patching.
    """
    return message_from_bytes(strip_envelope(raw), policy=email.policy.default)


def strip_envelope(raw: bytes) -> bytes:
    """Drop the leading mbox 'From ' envelope line, if present."""
    if raw.startswith(b"From "):
        nl = raw.find(b"\n")
        if nl != -1:
            return raw[nl + 1:]
    return raw


def describe(raw: bytes) -> dict:
    """Best-effort header summary; never raises."""
    try:
        msg = parse_headers(raw)
        return {
            "from": str(msg.get("From", "(unknown)")),
            "subject": str(msg.get("Subject", "(no subject)")),
            "date": str(msg.get("Date", "(no date)")),
            "message-id": str(msg.get("Message-ID", "")),
        }
    except Exception as exc:                # malformed mail must still deliver
        log.warning("[WARN] Could not parse headers: %s", exc)
        return {"from": "(unparsed)", "subject": "(unparsed)",
                "date": "", "message-id": ""}


# ---------------------------------------------------------------------------
# Gmail insert (using the import API to get scanning and classification)
# ---------------------------------------------------------------------------

def insert_message(service, raw: bytes) -> dict:
    """Insert a single message into the authenticated user's mailbox."""
    body = {
        "raw":      base64.urlsafe_b64encode(
            strip_envelope(raw)).decode("utf-8"
                                        ),
        "labelIds": ["INBOX", "UNREAD"],
    }
    return (
        service.users()
        .messages()
        .import_(
            userId="me",
            body=body,
            internalDateSource="receivedTime",
        )
        .execute()
    )


def http_status(exc: BaseException) -> int | None:
    """Extract the HTTP status from an HttpError, if there is one."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def retry_after(exc: BaseException) -> float | None:
    """Honour a Retry-After header expressed in seconds."""
    resp = getattr(exc, "resp", None)
    if resp is None:
        return None
    try:
        value = resp.get("retry-after")
    except Exception:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None                      # HTTP-date form: fall back to backoff


def is_retryable(exc: BaseException) -> bool:
    """True if another attempt has a realistic chance of succeeding."""
    if isinstance(exc, HttpError):
        return http_status(exc) in RETRYABLE_STATUS
    if isinstance(exc, TRANSIENT_EXCEPTIONS):
        return True
    return False


def insert_with_retry(service, raw: bytes, *,
                      max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                      initial_backoff: float = DEFAULT_INITIAL_BACKOFF,
                      max_backoff: float = DEFAULT_MAX_BACKOFF,
                      deadline: float = DEFAULT_DEADLINE,
                      sleep=time.sleep,
                      monotonic=time.monotonic) -> dict:
    """
    Insert a message, retrying transient failures with exponential backoff.

    Backoff uses "equal jitter" (half fixed, half random) so that a fleet of
    procmail deliveries hitting a Gmail outage does not resynchronise into a
    thundering herd.  Gives up when the attempt budget or the wall-clock
    deadline is exhausted, re-raising the last exception.
    """
    started = monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            return insert_message(service, raw)
        except Exception as exc:
            if not is_retryable(exc):
                raise
            if attempt >= max_attempts:
                log.error("[RETRY] Giving up after %d attempt(s): %s",
                          attempt, exc)
                raise

            backoff = min(max_backoff, initial_backoff * (2 ** (attempt - 1)))
            delay = backoff / 2 + random.uniform(0, backoff / 2)
            hinted = retry_after(exc)
            if hinted is not None:
                delay = max(delay, min(hinted, max_backoff))

            if monotonic() - started + delay > deadline:
                log.error("[RETRY] Retry deadline (%ds) reached: %s",
                          deadline, exc)
                raise

            log.warning(
                "[RETRY] Attempt %d/%d failed (%s); retrying in %.1fs",
                attempt, max_attempts, _short(exc), delay
            )
            sleep(delay)


def _short(exc: BaseException) -> str:
    status = http_status(exc)
    label = type(exc).__name__ if status is None else f"HTTP {status}"
    text = " ".join(str(exc).split())
    return f"{label}: {text[:180]}"


# ---------------------------------------------------------------------------
# Spool - the "never lose the message" backstop
# ---------------------------------------------------------------------------

def spool_path(spool_dir: str) -> Path:
    return Path(spool_dir).expanduser()


def spool_name(user_id: int) -> str:
    """
    Build a spool file name that carries its own destination account.

    Format: u<USER>-<ns timestamp>-<pid>-<random>.mbx

    The user ID is part of the name (not only the sidecar) so that --flush
    can always route a message back to the mailbox it was addressed to, even
    if the metadata file is lost, and so `ls` alone tells an admin whose mail
    is stuck.  The timestamp sorts oldest-first within each account.
    """
    return f"u{int(user_id)}-{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex[:8]}.mbx"


def spool_user(path: Path, meta: dict | None = None) -> int | None:
    """
    Recover the destination user ID for a spooled message.

    The sidecar is authoritative; the filename is the fallback that survives
    metadata loss.  Returns None when neither source can say, so the caller
    can refuse to guess rather than deliver someone else's mail to user 0.
    """
    if meta:
        try:
            return int(meta["user"])
        except (KeyError, TypeError, ValueError):
            pass
    match = SPOOL_NAME_RE.match(path.name)
    return int(match.group(1)) if match else None


SPOOL_NAME_RE = re.compile(r"^u(\d+)-")


def spool_message(spool_dir: str, raw: bytes, meta: dict, user_id: int) -> Path:
    """
    Park an undeliverable message on disk, durably.

    Written to a temp file in the same filesystem, fsync'd, then renamed into
    place, so a crash or a full disk can never leave a half-written message
    that --flush would happily deliver.  Files are plain mbox: worst case the
    admin can `cat` them into a mailbox by hand.  The name records which
    --user the message was destined for.
    """
    root = spool_path(spool_dir)
    tmp_dir = root / "tmp"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
    except OSError as exc:
        raise SpoolError(f"cannot create spool directory {root}: {exc}") from exc

    name = spool_name(user_id)
    tmp_file = tmp_dir / name
    final = root / name

    payload = raw if raw.startswith(b"From ") else _envelope_line(meta) + raw

    try:
        with open(tmp_file, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_file, 0o600)
        os.replace(tmp_file, final)
        _fsync_dir(root)
    except OSError as exc:
        _unlink(tmp_file)
        raise SpoolError(f"cannot write {final}: {exc}") from exc

    _write_sidecar(final, {
        "user": int(user_id),
        "first_seen": time.time(),
        "attempts": meta.get("attempts", 1),
        "last_error": meta.get("last_error", ""),
        "subject": meta.get("subject", ""),
        "from": meta.get("from", ""),
        "message-id": meta.get("message-id", ""),
    })
    return final


def _envelope_line(meta: dict) -> bytes:
    sender = (meta.get("from") or "gmail_insert").split()[-1].strip("<>")
    stamp = time.asctime(time.gmtime())
    return f"From {sender} {stamp}\n".encode("utf-8", "replace")


def _sidecar(path: Path) -> Path:
    return path.with_suffix(".json")


def _write_sidecar(path: Path, data: dict) -> None:
    """Metadata is best-effort - never let it block or undo a delivery."""
    try:
        tmp = _sidecar(path).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, _sidecar(path))
    except OSError as exc:
        log.warning("[WARN] Could not write spool metadata for %s: %s",
                    path, exc)


def _read_sidecar(path: Path) -> dict:
    try:
        return json.loads(_sidecar(path).read_text())
    except (OSError, ValueError):
        return {}


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_DIRECTORY)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def acquire_flush_lock(spool_dir: str):
    """Serialise --flush runs so overlapping cron jobs cannot double-send."""
    import fcntl
    root = spool_path(spool_dir)
    root.mkdir(parents=True, exist_ok=True)
    fh = open(root / ".flush.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    return fh


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Insert mbox message(s) from stdin into Gmail via the API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 gmail_insert.py < message.mbox
  cat archive.mbox | python3 gmail_insert.py
  python3 gmail_insert.py --debug --user 2 < message.mbox
  python3 gmail_insert.py --mbox < multiple_message.mbox
  python3 gmail_insert.py --json < message.mbox | jq .
  python3 gmail_insert.py --auth --user 2          # one-time authorisation
  python3 gmail_insert.py --flush                  # drain every user (cron)
  python3 gmail_insert.py --flush --user 2         # drain user 2 only

Exit codes:
  0   message delivered to Gmail, or safely spooled for a later --flush
  1   usage / input error (nothing was accepted, nothing was lost)
  75  EX_TEMPFAIL - delivery and spooling both failed; procmail should
      fall through to its next recipe and the MTA should requeue
        """,
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Produce debugging output",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Produce json output",
    )
    p.add_argument(
        "--user",
        type=int,
        default=None,
        metavar="USER",
        help=f"Integer user ID for credentials to use.  Defaults to "
             f"{DEFAULT_USER}.  With --flush, restricts the run to that one "
             f"account instead of every account with spooled mail",
    )
    p.add_argument(
        "--auth",
        action="store_true",
        help="Run the interactive OAuth flow, save the token, and exit",
    )
    p.add_argument(
        "--flush",
        action="store_true",
        help="Re-attempt delivery of spooled messages, then exit.  Every "
             "account is flushed with its own saved credentials unless "
             "--user is given",
    )
    p.add_argument(
        "--mbox",
        action="store_true",
        help="Allow multiple messages in mbox format in the input stream"
    )
    p.add_argument(
        "--credentials",
        metavar="FILE",
        help=f"Path to {CREDENTIALS_FILE} (default: CWD, then script directory)",
    )
    p.add_argument(
        "--token-dir",
        metavar="DIR",
        help="Directory holding token_USER.json (default: CWD, then script dir)",
    )
    p.add_argument(
        "--spool-dir",
        default=os.environ.get("GMAIL_INSERT_SPOOL", DEFAULT_SPOOL),
        metavar="DIR",
        help=f"Directory for undeliverable messages.  Defaults to {DEFAULT_SPOOL}",
    )
    p.add_argument(
        "--on-failure",
        choices=("spool", "tempfail"),
        default="spool",
        help="After retries are exhausted: park the message on disk (default) "
             "or exit EX_TEMPFAIL and let procmail/the MTA hold it",
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        metavar="N",
        help=f"Total API attempts per message.  Defaults to {DEFAULT_MAX_ATTEMPTS}",
    )
    p.add_argument(
        "--initial-backoff",
        type=float,
        default=DEFAULT_INITIAL_BACKOFF,
        metavar="SEC",
        help=f"First retry delay in seconds.  Defaults to {DEFAULT_INITIAL_BACKOFF}",
    )
    p.add_argument(
        "--max-backoff",
        type=float,
        default=DEFAULT_MAX_BACKOFF,
        metavar="SEC",
        help=f"Cap on the retry delay.  Defaults to {DEFAULT_MAX_BACKOFF}",
    )
    p.add_argument(
        "--retry-deadline",
        type=float,
        default=DEFAULT_DEADLINE,
        metavar="SEC",
        help="Wall-clock budget for retrying one message; keeps procmail from "
             f"stalling the MTA.  Defaults to {DEFAULT_DEADLINE}",
    )
    return p


def retry_kwargs(args) -> dict:
    return {
        "max_attempts": max(1, args.max_attempts),
        "initial_backoff": args.initial_backoff,
        "max_backoff": args.max_backoff,
        "deadline": args.retry_deadline,
    }


def connect(args, user_id: int, *, interactive: bool = False):
    """Build a service for one account.  Each --user has its own token."""
    return get_gmail_service(
        str(user_id),
        interactive=interactive,
        credentials_file=args.credentials,
        token_dir=args.token_dir,
    )


def deliver_user(args) -> int:
    """The account stdin deliveries are addressed to (--user, default 0)."""
    return DEFAULT_USER if args.user is None else args.user


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_auth(args) -> int:
    user_id = deliver_user(args)
    try:
        connect(args, user_id, interactive=True)
    except AuthUnavailable as exc:
        log.error("[ERROR] %s", exc)
        return 1
    log.warning("[AUTH] Authorisation complete for user %d.", user_id)
    return 0


def collect_spool(root: Path, only_user: int | None = None):
    """
    Group spooled messages by destination user, oldest first within each.
Returns (by_user, orphans).  Orphans are files whose user ID cannot be
    determined from either the sidecar or the name; they are reported rather
    than guessed at, since delivering one account's mail into another's
    mailbox is worse than leaving it on disk.
    """
    by_user: dict[int, list[tuple[Path, dict]]] = {}
    orphans: list[Path] = []

    for path in sorted(root.glob("*.mbx")):
        meta = _read_sidecar(path)
        user_id = spool_user(path, meta)
        if user_id is None:
            orphans.append(path)
            continue
        if only_user is not None and user_id != only_user:
            continue
        by_user.setdefault(user_id, []).append((path, meta))

    for items in by_user.values():
        items.sort(key=lambda item: item[0].name)     # timestamp-ordered
    return by_user, orphans


def cmd_flush(args) -> int:
    """
    Drain the spool, one account at a time.

    Every user with spooled mail is flushed using that user's own saved
    credentials; pass --user N to restrict the run to a single account.
    Safe to run from cron every few minutes.
    """
    root = spool_path(args.spool_dir)
    only_user = args.user                            # None => every account
    lock = acquire_flush_lock(args.spool_dir)
    if lock is None:
        log.info("[FLUSH] Another flush is already running; nothing to do.")
        return 0

    try:
        by_user, orphans = collect_spool(root, only_user)

        for path in orphans:
            log.error("[FLUSH] Cannot tell which user %s belongs to; leaving" +
                      " it in place. (Rename it u<USER>-... to route it)",
                      path.name)

        if not by_user:
            if not orphans:
                log.info("[FLUSH] Spool is empty.")
            return EX_TEMPFAIL if orphans else 0

        total = sum(len(items) for items in by_user.values())
        log.warning("[FLUSH] %d spooled message(s) in %s for user(s) %s",
                    total, root, ','.join(str(u) for u in sorted(by_user)))

        remaining = len(orphans)
        for user_id in sorted(by_user):
            items = by_user[user_id]
            log.warning("[FLUSH] user %d: %d message(s)", user_id, len(items))

            # A broken token for one account must not stall the others.
            try:
                service = connect(args, user_id)
            except AuthUnavailable as exc:
                log.error("[FLUSH] user %d: %s", user_id, exc)
                remaining += len(items)
                continue
            except Exception as exc:
                log.error("[FLUSH] user %d: authentication failed: %s",
                          user_id, _short(exc))
                remaining += len(items)
                continue

            for path, meta in items:
                try:
                    raw = path.read_bytes()
                except OSError as exc:
                    log.error("[ERROR] Cannot read %s: %s", path, exc)
                    remaining += 1
                    continue
                try:
                    result = insert_with_retry(service, raw, **retry_kwargs(args))
                except Exception as exc:
                    remaining += 1
                    meta["user"] = user_id
                    meta["attempts"] = int(meta.get("attempts", 0)) + 1
                    meta["last_error"] = _short(exc)
                    _write_sidecar(path, meta)
                    log.error("[FLUSH] user %d: still failing: %s: %s",
                              user_id, path.name, _short(exc))
                    continue
                log.warning("[FLUSH] user %d: delivered %s as %s", user_id,
                            path.name, result.get('id'))
                _unlink(_sidecar(path))
                _unlink(path)

        if remaining:
            log.error("[FLUSH] %d message(s) remain spooled in %s",
                      remaining, root)
            return EX_TEMPFAIL
        return 0
    finally:
        if lock is not None:
            lock.close()


def cmd_deliver(args) -> int:
    # All status/diagnostic output goes to stderr so stdout stays clean JSON
    # (useful for piping: python3 gmail_insert.py --json < msg.mbox | jq .)

    # --- Read stdin ---------------------------------------------------------
    if sys.stdin.isatty():
        log.error("[ERROR] No input detected. Pipe an mbox file into this script.")
        log.error("  Example: python3 gmail_insert.py < message.mbox")
        return 1

    log.info("[INFO] Reading mbox from stdin...")
    try:
        raw_input_bytes = sys.stdin.buffer.read()
    except OSError as exc:
        log.error("[ERROR] Could not read stdin: %s", exc)
        return EX_TEMPFAIL

    if not raw_input_bytes.strip():
        log.error("[ERROR] No messages found in input.")
        return 1
    if args.mbox:
        messages = split_mbox(raw_input_bytes)
    else:
        messages = [raw_input_bytes]

    log.info("[INFO] Parsed %d message(s).\n", len(messages))

    # --- Summarise what was parsed ------------------------------------------
    summaries = [describe(raw) for raw in messages]
    for i, info in enumerate(summaries, start=1):
        log.info("  [%d] From   : %s", i, info['from'])
        log.info("       Subject: %s", info['subject'])
        log.info("       Date   : %s", info['date'])
    log.info("")

    # --- Authenticate -------------------------------------------------------
    # Authentication failures are treated exactly like API failures: the mail
    # goes to the spool rather than to /dev/null.
    user_id = deliver_user(args)
    service = None
    auth_error = None
    try:
        service = connect(args, user_id)
    except AuthUnavailable as exc:
        auth_error = exc
        log.error("[ERROR] %s", exc)
    except Exception as exc:
        auth_error = exc
        log.error("[ERROR] Authentication failed: %s", _short(exc))

    # --- Insert each message ------------------------------------------------
    results = []
    inserted = 0
    spooled = 0
    lost = 0

    for i, (raw, info) in enumerate(zip(messages, summaries), start=1):
        subject = info["subject"]
        exc = auth_error
        result = None
        if service is not None:
            try:
                result = insert_with_retry(service, raw, **retry_kwargs(args))
                exc = None
            except Exception as err:         # HttpError and transport errors
                exc = err

        if result is not None:
            inserted += 1
            results.append({"status": "ok", **result})
            log.info("[OK] Message %d inserted", i)
            log.info("     Subject   : %s", subject)
            log.info("     Message ID: %s", result['id'])
            continue

        log.error("[ERROR] Message %d failed: %s", i, _short(exc))

        if args.on_failure == "tempfail":
            lost += 1
            results.append({"status": "error", "subject": subject,
                            "error": str(exc)})
            continue

        try:
            path = spool_message(args.spool_dir, raw,
                                 {**info, "attempts": 1,
                                  "last_error": _short(exc)},
                                 user_id)
            spooled += 1
            results.append({"status": "spooled", "subject": subject,
                            "user": user_id, "spool_file": str(path),
                            "error": str(exc)})
            log.error("[SPOOL] Message %d for user %d parked at %s " +
                      "- run 'gmail_insert.py --flush' to retry",
                      i, user_id, path)
        except SpoolError as spool_exc:
            lost += 1
            results.append({"status": "error", "subject": subject,
                            "error": f"{exc} / spool failed: {spool_exc}"})
            log.error("[FATAL] Message %d could not be spooled: {spool_exc}",
                      i, spool_exc)
            log.error("[FATAL] Exiting EX_TEMPFAIL so the message is requeued.")

    # --- Summary ------------------------------------------------------------
    log.info("[DONE] Inserted %d of %d message(s).", inserted, len(messages))
    if spooled:
        log.warning("[DONE] Spooled i%d message(s) for user %d to %s.",
                    spooled, user_id, args.spool_dir)
    if inserted:
        log.info("       Open Gmail in your browser to see them.")

    # JSON result to stdout for easy scripting / piping
    if args.json:
        print(json.dumps(results, indent=2))

    # Exit 0 only when every message is either in Gmail or safely on disk.
    return EX_TEMPFAIL if lost else 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = build_arg_parser()
    args   = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    if args.auth:
        return cmd_auth(args)
    if args.flush:
        return cmd_flush(args)
    return cmd_deliver(args)


if __name__ == "__main__":
    sys.exit(main())

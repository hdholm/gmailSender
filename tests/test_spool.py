"""
The spool - the "never lose the message" backstop.

Covers durability of the write, the u<USER>- naming contract that lets
--flush route each message back to the right account, and the grouping
--flush relies on.
"""

import base64
import os
from pathlib import Path

import support
import gmail_insert as g

META = {"from": "sender@example.com", "subject": "Test message", "attempts": 1,
        "last_error": "HTTP 500: Temporary System Problem."}


def test_spool_write_is_atomic_and_private():
    spool = support.temp_spool()
    raw = support.message("Durable")
    path = g.spool_message(spool, raw, META, 0)

    support.expect(path.exists(), "spool file missing")
    support.expect(oct(path.stat().st_mode)[-3:] == "600",
                   f"spool file should be mode 600, is {oct(path.stat().st_mode)[-3:]}")
    leftovers = list((Path(spool) / "tmp").glob("*"))
    support.expect(not leftovers, f"temp files left behind: {leftovers}")


def test_spooled_bytes_are_identical_to_stdin():
    """Whatever procmail handed us is what we must be able to hand back."""
    spool = support.temp_spool()
    raw = support.message("Byte for byte", body="Line one\nLine two\n")
    path = g.spool_message(spool, raw, META, 0)
    support.expect(path.read_bytes() == raw, "spooled bytes differ from input")


def test_envelope_line_is_synthesised_when_absent():
    """Spool files must be valid mbox so `cat` into a mailbox always works."""
    spool = support.temp_spool()
    raw = support.message("No envelope", envelope=False)
    path = g.spool_message(spool, raw, META, 0)
    written = path.read_bytes()
    support.expect(written.startswith(b"From "), "no mbox envelope line added")
    support.expect(written.endswith(raw), "original message body was altered")


def test_api_payload_strips_the_envelope_line():
    raw = support.message("Payload check")
    payload = base64.urlsafe_b64decode(base64.urlsafe_b64encode(g.strip_envelope(raw)).decode("utf-8"))
    support.expect(not payload.startswith(b"From "),
                   "mbox envelope line leaked into the API payload")
    support.expect(payload.startswith(b"From: Sender"),
                   "headers were mangled on the way to the API")


def test_unwritable_spool_raises_spool_error():
    """Which is what turns into EX_TEMPFAIL rather than a silent loss."""
    try:
        g.spool_message("/proc/definitely/not/writable", support.message(), META, 0)
    except g.SpoolError:
        return
    raise AssertionError("expected SpoolError for an unwritable spool directory")


def test_filename_encodes_the_user_id():
    spool = support.temp_spool()
    for user_id in (0, 2, 17):
        path = g.spool_message(spool, support.message(f"u{user_id}"), META, user_id)
        support.expect(path.name.startswith(f"u{user_id}-"),
                       f"expected a u{user_id}- prefix, got {path.name}")
        support.expect(path.name.endswith(".mbx"), f"bad extension: {path.name}")


def test_sidecar_records_user_and_error_context():
    spool = support.temp_spool()
    path = g.spool_message(spool, support.message(), META, 2)
    meta = g._read_sidecar(path)
    support.expect(meta["user"] == 2, "sidecar lost the user ID")
    support.expect(meta["subject"] == "Test message", "sidecar lost the subject")
    support.expect(meta["attempts"] == 1, "sidecar lost the attempt count")
    support.expect("500" in meta["last_error"], "sidecar lost the error")
    support.expect(meta["first_seen"] > 0, "sidecar lost the timestamp")


def test_user_is_recoverable_from_the_filename_alone():
    """The sidecar is best-effort, so the name has to be able to stand in."""
    spool = support.temp_spool()
    path = g.spool_message(spool, support.message(), META, 17)
    support.expect(g.spool_user(path, g._read_sidecar(path)) == 17,
                   "user not read from sidecar")

    os.unlink(g._sidecar(path))                       # simulate metadata loss
    support.expect(g.spool_user(path, g._read_sidecar(path)) == 17,
                   "user not recovered from the filename after sidecar loss")


def test_sidecar_wins_over_the_filename():
    spool = support.temp_spool()
    path = g.spool_message(spool, support.message(), META, 2)
    support.expect(g.spool_user(path, {"user": 5}) == 5,
                   "sidecar should be authoritative")


def test_unroutable_file_is_never_guessed_at():
    """Delivering one account's mail to another is worse than holding it."""
    spool = support.temp_spool()
    legacy = Path(spool) / "1785556921664013805-597-0f332428.mbx"
    legacy.write_bytes(support.message("Legacy"))

    support.expect(g.spool_user(legacy, {}) is None,
                   "an unprefixed file must not resolve to a user")
    by_user, orphans = g.collect_spool(Path(spool))
    support.expect(by_user == {}, "an unroutable file must not be grouped")
    support.expect(orphans == [legacy], f"expected 1 orphan, got {orphans}")


def test_collect_spool_groups_by_user():
    spool = support.temp_spool()
    for user_id in (0, 2, 17, 2):
        g.spool_message(spool, support.message(f"for {user_id}"), META, user_id)

    by_user, orphans = g.collect_spool(Path(spool))
    counts = {user: len(items) for user, items in by_user.items()}
    support.expect(counts == {0: 1, 2: 2, 17: 1}, f"bad grouping: {counts}")
    support.expect(not orphans, "unexpected orphans")


def test_collect_spool_can_filter_to_one_user():
    spool = support.temp_spool()
    for user_id in (0, 2, 17):
        g.spool_message(spool, support.message(f"for {user_id}"), META, user_id)

    by_user, _ = g.collect_spool(Path(spool), only_user=2)
    support.expect(sorted(by_user) == [2], f"--user filter leaked: {sorted(by_user)}")
    support.expect(len(by_user[2]) == 1, "wrong message count for user 2")


def test_messages_are_ordered_oldest_first_within_a_user():
    spool = support.temp_spool()
    written = [g.spool_message(spool, support.message(f"m{i}"), META, 1)
               for i in range(5)]
    by_user, _ = g.collect_spool(Path(spool))
    got = [path.name for path, _ in by_user[1]]
    support.expect(got == [p.name for p in written],
                   "spool is not drained in arrival order")


def test_flush_lock_serialises_concurrent_runs():
    """Overlapping cron runs must not both deliver the same message."""
    spool = support.temp_spool()
    first = g.acquire_flush_lock(spool)
    support.expect(first is not None, "first flush should take the lock")
    support.expect(g.acquire_flush_lock(spool) is None,
                   "a second concurrent flush must be refused")
    first.close()
    second = g.acquire_flush_lock(spool)
    support.expect(second is not None, "lock not released")
    second.close()

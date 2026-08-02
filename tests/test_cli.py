"""
End-to-end tests through the command line.

These run gmail_insert in a subprocess so the exit codes are observed the
way procmail and cron observe them - that contract is the whole reason the
spool exists.  See tests/driver.py for the scripted fake API.

The procmail contract under test:
    0   delivered to Gmail, or safely spooled for a later --flush
    1   usage/input error: nothing accepted, nothing lost
    75  EX_TEMPFAIL: delivery and spooling both failed; requeue it
"""

from pathlib import Path

import support

FAST = ["--initial-backoff", "0.01", "--max-backoff", "0.02"]


def base_args(spool, tokens, *extra):
    return ["--spool-dir", str(spool), "--token-dir", str(tokens), *FAST, *extra]


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def test_successful_delivery_exits_zero():
    spool, tokens = support.temp_spool(), support.token_dir(0)
    done = support.run_cli(base_args(spool, tokens, "--json"),
                           stdin=support.message("Straight through"))
    support.expect(done.returncode == 0, f"exit {done.returncode}: {done.stderr}")
    result = support.json_output(done)[0]
    support.expect(result["status"] == "ok", f"unexpected result {result}")
    support.expect(not support.spool_files(spool), "nothing should be spooled")


def test_transient_failures_recover_without_spooling():
    """Issue #1: two 500s in a row must not cost us the message."""
    spool, tokens = support.temp_spool(), support.token_dir(0)
    done = support.run_cli(base_args(spool, tokens, "--json"),
                           mode="flaky:2", stdin=support.message("Flaky"))
    support.expect(done.returncode == 0, f"exit {done.returncode}: {done.stderr}")
    support.expect(support.json_output(done)[0]["status"] == "ok",
                   "should have recovered on retry")
    support.expect(b"[RETRY]" in done.stderr, "retries were not logged")
    support.expect(not support.spool_files(spool), "should not have spooled")


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def test_persistent_failure_spools_and_exits_zero():
    """Exit 0 is deliberate: the message is ours now, so procmail is done."""
    spool, tokens = support.temp_spool(), support.token_dir(0)
    raw = support.message("Parked")
    done = support.run_cli(base_args(spool, tokens, "--json", "--user", "2"),
                           mode="always_fail", stdin=raw)

    support.expect(done.returncode == 0, f"exit {done.returncode}: {done.stderr}")
    result = support.json_output(done)[0]
    support.expect(result["status"] == "spooled", f"unexpected result {result}")
    support.expect(result["user"] == 2, "JSON output lost the user ID")

    files = support.spool_files(spool)
    support.expect(len(files) == 1, f"expected 1 spooled file, got {files}")
    support.expect(files[0].name.startswith("u2-"), f"bad name {files[0].name}")
    support.expect(files[0].read_bytes() == raw, "spooled bytes differ from stdin")


def test_on_failure_tempfail_exits_75_without_spooling():
    spool, tokens = support.temp_spool(), support.token_dir(0)
    done = support.run_cli(
        base_args(spool, tokens, "--on-failure", "tempfail"),
        mode="always_fail", stdin=support.message("Requeue me"))
    support.expect(done.returncode == 75, f"expected 75, got {done.returncode}")
    support.expect(not support.spool_files(spool),
                   "tempfail mode must not spool")


def test_unwritable_spool_exits_75():
    """The one case we hand the message back rather than accept it."""
    tokens = support.token_dir(0)
    done = support.run_cli(
        ["--spool-dir", "/proc/definitely/not/writable",
         "--token-dir", str(tokens), *FAST],
        mode="always_fail", stdin=support.message("Nowhere to go"))
    support.expect(done.returncode == 75, f"expected 75, got {done.returncode}")


def test_empty_input_is_a_usage_error():
    spool, tokens = support.temp_spool(), support.token_dir(0)
    done = support.run_cli(base_args(spool, tokens), stdin=b"")
    support.expect(done.returncode == 1, f"expected 1, got {done.returncode}")
    support.expect(not support.spool_files(spool), "nothing should be spooled")


def test_missing_token_spools_instead_of_prompting():
    """
    Regression guard: reaching the interactive OAuth flow from an MDA would
    block procmail forever holding the message.  No token means spool.
    """
    spool, tokens = support.temp_spool(), support.token_dir()      # no tokens
    done = support.run_cli(base_args(spool, tokens, "--json"),
                           stdin=support.message("No token"), timeout=20)
    support.expect(done.returncode == 0, f"exit {done.returncode}: {done.stderr}")
    support.expect(support.json_output(done)[0]["status"] == "spooled",
                   "an unauthenticated run must spool, not fail open")
    support.expect(b"--auth" in done.stderr, "the operator was not told how to fix it")


# ---------------------------------------------------------------------------
# Multi-user flush
# ---------------------------------------------------------------------------

def spool_for_users(spool, tokens, pairs):
    """Force each (user, subject) into the spool via a failing API."""
    for user_id, subject in pairs:
        done = support.run_cli(
            base_args(spool, tokens, "--json", "--user", str(user_id)),
            mode="always_fail", stdin=support.message(subject))
        support.expect(done.returncode == 0, f"spooling failed: {done.stderr}")


def test_flush_uses_each_users_own_credentials():
    spool = support.temp_spool()
    tokens = support.token_dir(0, 2, 17)
    audit = support.audit_file()
    spool_for_users(spool, tokens, [(0, "for-zero"), (2, "for-two"),
                                    (17, "for-seventeen"), (2, "second-for-two")])

    done = support.run_cli(base_args(spool, tokens, "--flush"), audit=audit)
    support.expect(done.returncode == 0, f"exit {done.returncode}: {done.stderr}")

    delivered = {(row["subject"], row["token"]) for row in support.read_audit(audit)}
    support.expect(delivered == {
        ("for-zero", "token_0.json"),
        ("for-two", "token_2.json"),
        ("second-for-two", "token_2.json"),
        ("for-seventeen", "token_17.json"),
    }, f"wrong token used somewhere: {delivered}")

    support.expect(not support.spool_files(spool), "spool not drained")
    support.expect(not list(Path(spool).glob("*.json")), "sidecars left behind")


def test_flush_user_restricts_to_one_account():
    spool = support.temp_spool()
    tokens = support.token_dir(0, 2)
    audit = support.audit_file()
    spool_for_users(spool, tokens, [(0, "for-zero"), (2, "for-two")])

    done = support.run_cli(base_args(spool, tokens, "--flush", "--user", "2"),
                           audit=audit)
    support.expect(done.returncode == 0, f"exit {done.returncode}: {done.stderr}")

    rows = support.read_audit(audit)
    support.expect([r["subject"] for r in rows] == ["for-two"],
                   f"--user 2 delivered the wrong set: {rows}")
    remaining = support.spool_names(spool)
    support.expect(len(remaining) == 1 and remaining[0].startswith("u0-"),
                   f"user 0's mail should be untouched, spool holds {remaining}")


def test_one_broken_account_does_not_block_the_others():
    spool = support.temp_spool()
    tokens = support.token_dir(0, 2)                  # deliberately no token_3
    audit = support.audit_file()
    spool_for_users(spool, tokens, [(0, "for-zero"), (2, "for-two"),
                                    (3, "for-three")])

    done = support.run_cli(base_args(spool, tokens, "--flush"), audit=audit)
    support.expect(done.returncode == 75,
                   f"a stuck account should report EX_TEMPFAIL, got {done.returncode}")

    delivered = {row["subject"] for row in support.read_audit(audit)}
    support.expect(delivered == {"for-zero", "for-two"},
                   f"healthy accounts should still flush: {delivered}")

    remaining = support.spool_names(spool)
    support.expect(len(remaining) == 1 and remaining[0].startswith("u3-"),
                   f"user 3's mail should be held, spool holds {remaining}")
    support.expect(b"user 3" in done.stderr, "the stuck account was not reported")


def test_held_mail_is_delivered_after_the_account_is_authorised():
    spool = support.temp_spool()
    tokens = support.token_dir(0)                     # no token_3 yet
    audit = support.audit_file()
    spool_for_users(spool, tokens, [(3, "for-three")])

    first = support.run_cli(base_args(spool, tokens, "--flush"), audit=audit)
    support.expect(first.returncode == 75, "expected the message to be held")
    support.expect(len(support.spool_files(spool)) == 1, "message should still be spooled")

    (Path(tokens) / "token_3.json").write_text("{}")  # operator runs --auth
    second = support.run_cli(base_args(spool, tokens, "--flush"), audit=audit)
    support.expect(second.returncode == 0, f"exit {second.returncode}: {second.stderr}")
    support.expect([r["subject"] for r in support.read_audit(audit)] == ["for-three"],
                   "held message not delivered after authorisation")
    support.expect(not support.spool_files(spool),
                   "spool should be empty once the outage clears")


def test_flush_retries_and_records_attempts_while_the_api_is_down():
    spool = support.temp_spool()
    tokens = support.token_dir(0)
    spool_for_users(spool, tokens, [(0, "stubborn")])

    done = support.run_cli(base_args(spool, tokens, "--flush"), mode="always_fail")
    support.expect(done.returncode == 75, f"expected 75, got {done.returncode}")
    support.expect(len(support.spool_files(spool)) == 1,
                   "a failed flush must leave the message alone")

    import json
    meta = json.loads(next(Path(spool).glob("*.json")).read_text())
    support.expect(meta["attempts"] == 2,
                   f"attempt counter should advance, got {meta['attempts']}")
    support.expect("500" in meta["last_error"], "last error not recorded")


def test_flush_on_an_empty_spool_is_a_no_op():
    spool, tokens = support.temp_spool(), support.token_dir(0)
    done = support.run_cli(base_args(spool, tokens, "--flush"))
    support.expect(done.returncode == 0, f"exit {done.returncode}: {done.stderr}")


def test_flush_reports_unroutable_files_without_delivering_them():
    spool, tokens = support.temp_spool(), support.token_dir(0)
    audit = support.audit_file()
    orphan = Path(spool) / "1785556921664013805-597-0f332428.mbx"
    orphan.write_bytes(support.message("Orphaned"))

    done = support.run_cli(base_args(spool, tokens, "--flush"), audit=audit)
    support.expect(done.returncode == 75,
                   f"an unroutable file should report EX_TEMPFAIL, got {done.returncode}")
    support.expect(not support.read_audit(audit),
                   "an unroutable file must not be delivered to a guessed account")
    support.expect(orphan.exists(), "an unroutable file must be left in place")

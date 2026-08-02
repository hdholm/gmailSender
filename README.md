# Push email to Gmail using Import API

Eventually read one or more email messages in **mbox format from standard
input** and inserts each one directly into a Gmail mailbox via the
[Gmail API `messages.import`](https://developers.google.com/gmail/api/reference/rest/v1/users.messages/import)
endpoint — no SMTP, no sending, no "Sent" folder.

For the moment it works for exactly one email message in mbox format which is
perfectly adequate for the purpose of being a program that procmail can pipe
messages into to deliver to gmail.

---

## Quick start

```bash
# Single message
python3 gmail_insert.py < message.mbox

# Whole mbox archive (inserts every message) - TBD implementation
cat archive.mbox | python3 gmail_insert.py

# Pipe JSON results to jq
python3 gmail_insert.py < message.mbox | jq .

# Retry anything the API refused earlier, for every account (run from cron)
python3 gmail_insert.py --flush

# ...or for one account only
python3 gmail_insert.py --flush --user 2

# One-time interactive authorisation (never do this from procmail)
python3 gmail_insert.py --auth --user 0

# In a procmail entry something like
:0 w
| /venv/bin/python3 gmail_insert.py
```

---

## Reliability: retries, spooling and exit codes

The Gmail API returns transient errors — most often
`HTTP 500 "Temporary System Problem."` (`reason: backendError`) — and a
message handed over by procmail must never be dropped because of one.
Three layers protect it:

1. **Automatic retries.** `408`, `429` and `5xx` responses, plus socket
   timeouts, connection resets and TLS errors, are retried with exponential
   backoff and jitter, honouring `Retry-After`. Permanent errors (`400`,
   `403`, `404`) are not retried. Tunable with `--max-attempts`,
   `--initial-backoff`, `--max-backoff` and `--retry-deadline`; the deadline
   bounds how long procmail is made to wait.
2. **On-disk spool.** If the retries are exhausted — or authentication is
   unavailable — the original bytes are written to `--spool-dir`
   (default `~/.gmail_insert/spool`, override with `$GMAIL_INSERT_SPOOL`)
   with an atomic write/fsync/rename, and the script exits **0**. Procmail
   treats the delivery as complete because the message is now safely ours.
   Spool files are plain mbox, so `cat`-ing them into a mailbox is always
   available as an escape hatch.
3. **EX_TEMPFAIL.** If even the spool write fails, the script exits **75**,
   so procmail falls through to its next recipe and the MTA requeues.

### The spool is per-account

A single `.procmailrc` can feed several Gmail accounts by piping to
different `--user` values, so the spool has to remember which account each
stranded message was headed for. The destination is written into the file
name:

```
~/.gmail_insert/spool/
├── u0-1785556921664013805-4711-0f332428.mbx     # --user 0
├── u0-1785556921664013805-4711-0f332428.json    # metadata sidecar
└── u2-1785556934120884301-4713-9ac10be2.mbx     # --user 2
```

`u<USER>-<timestamp>-<pid>-<random>.mbx`. The user ID is recorded in *both*
the name and the sidecar: the sidecar is authoritative, and the name is the
fallback that survives metadata loss. The timestamp keeps each account's
mail in arrival order. A file whose account cannot be determined from
either source is reported and left alone rather than guessed at — putting
one account's mail in another's mailbox is worse than leaving it on disk.
To adopt such a file, rename it with a `u<USER>-` prefix.

Drain the spool from cron:

```cron
*/10 * * * * /venv/bin/python3 /path/to/gmail_insert.py --flush
```

`--flush` walks every account that has spooled mail and authenticates each
one with its own `token_USER.json`, oldest message first. One account with
an expired or missing token does not stall the rest — its messages stay
spooled and the run reports `EX_TEMPFAIL`, so the next cron pass picks them
up once you have re-run `--auth --user N`. Add `--user N` to limit a run to
a single account.

`--flush` takes an exclusive lock, so overlapping cron runs cannot
double-deliver. A spooled message is deleted only after Gmail accepts it.

### Recommended procmail recipe

```procmail
:0 w
| /venv/bin/python3 /path/to/gmail_insert.py

# Only reached when the script exits non-zero (EX_TEMPFAIL): keep a local
# copy rather than losing the mail.
:0 e
$DEFAULT
```

The `w` flag matters — without it procmail ignores the exit status
entirely, and a failed delivery really is a lost message. Run procmail with
`-t` if you would rather the MTA requeue on EX_TEMPFAIL.

| Exit code | Meaning |
| --------- | ------- |
| `0` | Delivered to Gmail, or safely spooled for a later `--flush` |
| `1` | Usage/input error — nothing was accepted, nothing was lost |
| `75` | `EX_TEMPFAIL` — delivery *and* spooling failed; requeue it |

> **Note:** a retry after a timeout could in principle insert a message
> twice, if the first attempt reached Gmail but the response never came
> back. The `gmail.insert` scope is write-only, so the script cannot check
> for itself. A rare duplicate is a far better failure mode than lost mail.

---

## Basics of mbox format

An mbox file is plain text. Each message is preceded by a **"From " envelope
line** (note the space after "From"):

```
From alice@example.com Mon Jan  1 12:00:00 2024
From: Alice <alice@example.com>
To: Bob <bob@example.com>
Subject: Hello
Date: Mon, 1 Jan 2024 12:00:00 +0000
Content-Type: text/plain

Message body here.

From bob@example.com Mon Jan  1 13:00:00 2024
From: Bob <bob@example.com>
To: Alice <alice@example.com>
Subject: Re: Hello
Date: Mon, 1 Jan 2024 13:00:00 +0000
Content-Type: text/plain

Thanks!
```

The script also accepts a **bare RFC-2822 message** with no "From " envelope
line (common when copying a single message out of another tool).

### Minimal single-message example

Save the following as `test.mbox`:

```
From: sender@example.com
To: you@gmail.com
Subject: Test via mbox insert
Date: Tue, 07 Apr 2026 10:00:00 +0000
Content-Type: text/plain

Hello! This was inserted via the Gmail API.
```

Then run:

```bash
python3 gmail_insert.py < test.mbox
```

---

## Prerequisites

### Python 3.13+

Testing with Python 3.10 causes a traceback that is not present in 3.13 when
parsing some email headers - notibly some originating from Microsoft.

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
```

### Install dependencies

```bash
pip install -r requirements.txt
```

If you are on a server which doesn't provide you with the abilty to install
python (or python venv) then you can install miniconda and do something like
this:

```bash
conda config --add channels conda-forge
conda create --file gmail_insert/requirements.txt -n genv
/path/to/miniconda3/bin/conda run -n genv --no-capture-output gmail_insert.py --debug < mail.mbx
```

### Enable the Gmail API & create credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create
   (or select) a project.
2. **APIs & Services → Library** — enable **Gmail API**.
3. **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**.
4. Application type: **Desktop App**.
5. Download the JSON and save it as **`credentials.json`** in the same
   directory as the script.
6. **APIs & Services → OAuth consent screen → Test users** — add your Gmail
   address (required if/while the app is in *Testing* mode).
7. You can channge from testing to production and still won't need to go
   through app verification as long as there are fewer than 100 users **ever**.

If you are sending to different gmail accounts you can use `--user` with an
integer (the default is 0) to designate each user.  You will need to
authenticate each one to create a token file for that account.  Only the one
credential file is needed.

---

## Output design

Diagnostic/status lines go to **stderr**; a JSON array of results goes to
**stdout**, so you can pipe cleanly if, and only if, `--debug` is used:

```bash
python3 gmail_insert.py --debug < archive.mbox | jq '.[].id'
```

Success entry:

```json
{
  "status": "ok",
  "id": "18f3a2b1c4d5e6f7",
  "threadId": "18f3a2b1c4d5e6f7",
  "labelIds": ["UNREAD", "INBOX"]
}
```

Error entry:

```json
{
  "status": "error",
  "subject": "My subject",
  "error": "400 Bad Request ..."
}
```

---

## Files

| File | Description |
|---|---|
| `gmail_insert.py` | Main script |
| `credentials.json` | OAuth client secret — **download from GCP, never commit** |
| `token_USERID.json` | Cached auth token — auto-created on first run. USERID is just an integer digit used to identify unique gmail accounts |
| `requirements.txt` | Python dependencies |
| `.gitignore` | Excludes credentials and token from version control |
| spool dir | `u<USER>-...mbx` messages the API would not accept yet; drained by `--flush` |
| `tests/` | Regression suite; runs with no dependencies |

---

## Testing

```bash
python3 tests/run_tests.py            # everything
python3 tests/run_tests.py -k backoff # tests whose name matches
python3 tests/run_tests.py spool cli  # selected modules
python3 tests/run_tests.py -v         # tracebacks for failures
pytest tests/                         # if you prefer pytest
```

The suite needs nothing but a Python interpreter — no network, no OAuth, no
credentials, and not even the Google client libraries. `tests/stubs/`
provides just enough of `googleapiclient`, `google.auth` and
`google_auth_oauthlib` to drive the script, and `tests/driver.py` replaces
`insert_message()` with a scripted fake that can succeed, fail with a given
HTTP status, or fail a set number of times before succeeding.

| File | Covers |
| ---- | ------ |
| `tests/test_retry.py` | Which failures are retried, backoff growth and jitter, `Retry-After`, the wall-clock deadline |
| `tests/test_spool.py` | Atomic/private spool writes, byte fidelity, `u<USER>-` naming, grouping and ordering, the flush lock |
| `tests/test_cli.py` | The procmail exit-code contract and multi-account `--flush`, end to end in a subprocess |

The CLI tests run the script as a subprocess so exit codes are observed
exactly as procmail and cron see them. Each test gets its own temporary
spool and token directory, so they are order-independent and can run in any
subset. Subprocesses are killed after 60s: a hang — the failure mode where
an MDA blocks on an interactive OAuth prompt — is reported as a failure
rather than stalling the suite.

Regressions worth keeping an eye on, each pinned by a named test: a 500
becoming non-retryable, a 4xx becoming retryable, transport errors escaping
the retry loop, the user ID falling out of a spool file name, `--flush`
authenticating every account with user 0's token, an unroutable spool file
being delivered to a guessed account, and a spool write failure exiting 0
instead of 75.

---

## How `messages.import` differs from `messages.insert` and `messages.send`

| | `import` | `insert` | `send` |
|---|---|---|---|
| Delivers to recipients? | No | No | Yes |
| Appears in Sent? | No | No | Yes |
| Arbitrary labels? | Some | Yes | Limited |
| Arbitrary From/Date? | ? | Yes | Must match account |
| Filtering to labels and spam? | Yes | No | No |
| Use-case | Import, testing, archiving | Import, testing, archiving | Actual delivery |

---

## Security notes

- **Never commit** `credentials.json` or `token*.json` — each should be 
  covered by `.gitignore`.
- The `gmail.insert` scope is write-only; the script cannot read or delete
  existing mail.

## Background

Google has deprecated and is soon turning off the ability to get email from
other servers via POP (and has never used IMAP.) Forwarding messages via
SMTP causes all kinds of issues including, but not limited to DKIM, SPF, and 
DMARC failures.  So I turned to their API and after a little (more than I
wanted) research I came up with this.

### Credits

- While seaching for a way to send email to Google, I found **Jeremy Ephron
  Barenholtz**'s github repository at
  https://github.com/jeremyephron/simplegmail which provided
  some insight into the gmail API and the genesis of this idea.
- **Anthropic's Claude AI** provided two proof of concept attempts that were
  close to functional and provided even more insight into the operation of the
  gmail API, but were broken in various ways.  Claude was also used to update
  to help update to the more modern EmailMessage class in python which fixed
  a number of the bugs in the previous Claude versions.
- Claude attempted to build a version (still on the horizon) that took both
  single messages and multiple message mboxen on standard input.  Unfortunately,
  it did that by using python 2's PortableUnixMailbox. But Python 3's mbox uses
  a pathname not a file-like-object. **Enrico Zini** seems to have a way
  forward in this blog:
  https://www.enricozini.org/blog/2019/debian/python-hacks-opening-a-compressed-mailbox/

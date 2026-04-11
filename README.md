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

# Whole mbox archive (inserts every message)
cat archive.mbox | python3 gmail_insert.py

# Custom labels
python3 gmail_insert.py --labels INBOX UNREAD IMPORTANT < message.mbox

# Preview without inserting
python3 gmail_insert.py --dry-run < message.mbox

# Pipe JSON results to jq
python3 gmail_insert.py < message.mbox | jq .

# In a procmail entry something like
:0
| /venv/bin/python3 gmail_insert.py
```

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

### Python 3.10+

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
  gmail API.
- My first attempts failed with utf-8 messages and **Dieter Maurer**'s bug fix
  at https://github.com/python/cpython/issues/85479 showed me the way forward.
- Claude attempted to build a version (still on the horizon) that took both
  single messages and multiple message mboxs on standard input.  Unfortunately,
  it did that by using python 2's PortableUnixMailbox. But Python 3's mbox uses
  a pathname not a file-like-object. **Enrico Zini** seems to have a way
  forward in this blog:
  https://www.enricozini.org/blog/2019/debian/python-hacks-opening-a-compressed-mailbox/

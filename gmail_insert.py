#!/usr/bin/env python3
"""
Gmail API - mbox stdin → Gmail Insert
======================================
Reads one or more messages in mbox format from standard input and inserts
each one directly into the authenticated user's Gmail mailbox using the
Gmail API's messages.insert method (no SMTP, no sending).

Usage:
    python3 gmail_insert.py < single_message.mbox
    cat archive.mbox | python3 gmail_insert.py
    python3 gmail_insert.py < messages.mbox

Options:
    --debug    Produce debugging output
    --user ID  Integer identifier of credentials file

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
    4. Run the script — a browser window opens for first-time auth

"""

import argparse
import base64
import email
import io
import json
import logging
import mailbox
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Needed to account for  https://github.com/python/cpython/issues/85479
from copy import copy
from io import BytesIO
from email.message import Message
from email.generator import BytesGenerator, _has_surrogates
from email._policybase import Compat32

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/gmail.insert"]
CREDENTIALS_FILE = "credentials.json"   # OAuth2 client secret downloaded from GCP

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_gmail_service(cred_id:str):
    """Authenticate and return an authorised Gmail API service object."""
    creds            = None
    token_file       = "token_" + cred_id + ".json"

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                log.error("[ERROR] 'credentials.json' not found.")
                log.error("  -> Download OAuth 2.0 credentials from Google Cloud Console")
                log.error(f"     and save them as '{CREDENTIALS_FILE}' in this directory.")
                sys.exit(1)

            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_file, "w") as fh:
            fh.write(creds.to_json())
        log.info(f"[AUTH] Token saved to {token_file}")

    return build("gmail", "v1", credentials=creds)

# ---------------------------------------------------------------------------
# mbox parsing
# ---------------------------------------------------------------------------

#This is a mess because the email module in python is a mess see:
#    https://github.com/python/cpython/issues/85479
#and
#    email.mbox only takes a file PATH not a file handle,
#    https://www.enricozini.org/tags/hacks/
#
class StreamMbox(mailbox.mbox):
    """
    mailbox.mbox does not support opening a stream, which is sad.

    This is a subclass that works around it
    """
    def __init__(self, fd: BinaryIO, factory=None, create: bool = True):
        # Do not call parent __init__, just redo everything here to be able to
        # open a stream. This will need to be re-reviewed for every new version
        # of python's stdlib.

        # Mailbox constructor
        self._path = None
        self._factory = factory

        # _singlefileMailbox constructor
        self._file = fd
        self._toc = None
        self._next_key = 0
        self._pending = False       # No changes require rewriting the file.
        self._pending_sync = False  # No need to sync the file
        self._locked = False
        self._file_length = None    # Used to record mailbox size

        # mbox constructor
        self._message_factory = mailbox.mboxMessage

    def flush(self):
        raise NotImplementedError("The mbox is readonly.")

class FixedBytesGenerator(BytesGenerator):
    '''Modify broken built-in BytesGenerator to account for utf-8 messages.'''
    def _handle_text(self, msg):
        payload = msg._payload
        if payload is None:
            return
        charset = msg.get_param("charset")
        if charset is not None \
               and not self.policy.cte_type=='7bit' \
               and not _has_surrogates(payload):
            msg = copy(msg)
            msg._payload = payload.encode(charset).decode(
                "ascii", "surrogateescape")
        super()._handle_text(msg)
                
    _writeBody = _handle_text


class FixedMessage(Message):
    '''Modify built-in Message class to use FixedBytesGenerator.'''
    def as_bytes(self, unixfrom=False, policy=None):
        policy = self.policy if policy is None else policy
        fp = BytesIO()
        g = FixedBytesGenerator(fp, mangle_from_=False, policy=policy)
        g.flatten(self, unixfrom=unixfrom)
        return fp.getvalue()

# A policy to use the corrected classes above to deal with utf-8 messages
fixed_policy = Compat32(message_factory=FixedMessage)

def parse_mbox_from_stdin() -> list:
    """
    Read mbox data from stdin and return a list of email.message.Message objects.

    Handles two cases:
      1. Proper mbox with one or more "From " separator lines.
      2. A bare RFC-2822 message with no "From " line (single message).
    """
    raw     = sys.stdin.buffer.read()
    decoded = raw.decode("utf-8", errors="replace")

    # If there is no mbox "From " envelope line, treat the whole input as a
    # single RFC-2822 message.
    #if not decoded.startswith("From "):
    #    return [email.message_from_string(decoded)]
    # Removed check becuase procmail pipes single mbox formatted messages
    return [email.message_from_string(decoded, policy=fixed_policy)]
    # Parse as a proper mbox stream using the stdlib mailbox module.
    # PortableUnixMailbox accepts a file-like object directly.
    #mbox = StreamMbox(
    #    io.StringIO(decoded),
    #    factory=email.message_from_file,
    #)
    #return list(mbox)
    log.error("[ERROR] multiple message mbox TBD.")

# ---------------------------------------------------------------------------
# Gmail insert (using the import API to get scanning and classification)
# ---------------------------------------------------------------------------

def message_to_raw(msg) -> str:
    """Base64url-encode an email.message.Message for the Gmail API."""
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

def insert_message(service, msg) -> dict:
    """Insert a single message into the authenticated user's mailbox."""
    body = {
        "raw":      message_to_raw(msg),
        "labelIds": ['INBOX', 'UNREAD'],

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
  python3 gmail_insert.py --debug < message.mbox | jq .
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
        default=0,
        metavar="USER",
        help="Integer user ID for credentials to use.  Defaults to 0",
    )
    return p

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = build_arg_parser()
    args   = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    # All status/diagnostic output goes to stderr so stdout stays clean JSON
    # (useful for piping: python3 gmail_insert.py < msg.mbox | jq .)

    # --5- Read stdin ---------------------------------------------------------
    if sys.stdin.isatty():
        log.error("[ERROR] No input detected. Pipe an mbox file into this script.")
        log.error("  Example: python3 gmail_insert.py < message.mbox")
        sys.exit(1)

    log.info("[INFO] Reading mbox from stdin...")
    messages = parse_mbox_from_stdin()

    if not messages:
        log.error("[ERROR] No messages found in input.")
        sys.exit(1)

    log.info(f"[INFO] Parsed {len(messages)} message(s).\n")

    # --- Summarise what was parsed ------------------------------------------
    for i, msg in enumerate(messages, start=1):
        log.info(f"  [{i}] From   : {msg.get('From',    '(unknown)')}")
        log.info(f"       Subject: {msg.get('Subject', '(no subject)')}")
        log.info(f"       Date   : {msg.get('Date',    '(no date)')}")
    log.info("")

    # --- Authenticate -------------------------------------------------------
    service  = get_gmail_service(str(args.user))

    # --- Insert each message ------------------------------------------------
    results  = []
    inserted = 0

    for i, msg in enumerate(messages, start=1):
        # emsg = email.message_from_string(msg)
        subject = msg.get("Subject", "(no subject)")
        try:
            result = insert_message(
                service,
                msg
            )
            inserted += 1
            results.append({"status": "ok", **result})
            log.info(f"[OK] Message {i} inserted")
            log.info(f"     Subject   : {subject}")
            log.info(f"     Message ID: {result['id']}")
        except HttpError as exc:
            results.append({"status": "error", "subject": subject, "error": str(exc)})
            log.error(f"[ERROR] Message {i} failed: {exc}")
            log.info("")

    #---- Summary ------------------------------------------------------------
    log.info(f"[DONE] Inserted {inserted} of {len(messages)} message(s).")
    if inserted:
        log.info("       Open Gmail in your browser to see them.")

    # JSON result to stdout for easy scripting / piping
    if args.json:
        print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()

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
import email.policy
import json
import logging
import os
import sys

from email import message_from_bytes
from email.message import EmailMessage

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/gmail.insert"]
CREDENTIALS_FILE = "credentials.json"   # OAuth2 client secret downloaded from GCP

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_gmail_service(cred_id: str):
    """Authenticate and return an authorised Gmail API service object."""
    creds      = None
    token_file = "token_" + cred_id + ".json"

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

def parse_mbox_from_stdin() -> list[EmailMessage]:
    """
    Read mbox data from stdin and return a list of EmailMessage objects.

    Uses email.policy.default so the result is a modern EmailMessage rather
    than the legacy Message class.  UTF-8 bodies and headers are handled
    correctly without any monkey-patching.

    Eventually handle two cases:
      1. Proper mbox with one or more "From " separator lines. (Still TBD.)
      2. A bare RFC-2822 message with no "From " line (single message).
    """
    raw = sys.stdin.buffer.read()

    msg = message_from_bytes(raw, policy=email.policy.default)
    return [msg]

# ---------------------------------------------------------------------------
# Gmail insert (using the import API to get scanning and classification)
# ---------------------------------------------------------------------------

def message_to_raw(msg: EmailMessage) -> str:
    """Base64url-encode an EmailMessage for the Gmail API. """
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

def insert_message(service, msg: EmailMessage) -> dict:
    """Insert a single message into the authenticated user's mailbox."""
    body = {
        "raw":      message_to_raw(msg),
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

    # --- Read stdin ---------------------------------------------------------
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
    service = get_gmail_service(str(args.user))

    # --- Insert each message ------------------------------------------------
    results  = []
    inserted = 0

    for i, msg in enumerate(messages, start=1):
        subject = msg.get("Subject", "(no subject)")
        try:
            result = insert_message(service, msg)
            inserted += 1
            results.append({"status": "ok", **result})
            log.info(f"[OK] Message {i} inserted")
            log.info(f"     Subject   : {subject}")
            log.info(f"     Message ID: {result['id']}")
        except HttpError as exc:
            results.append({"status": "error", "subject": subject, "error": str(exc)})
            log.error(f"[ERROR] Message {i} failed: {exc}")
            log.info("")

    # --- Summary ------------------------------------------------------------
    log.info(f"[DONE] Inserted {inserted} of {len(messages)} message(s).")
    if inserted:
        log.info("       Open Gmail in your browser to see them.")

    # JSON result to stdout for easy scripting / piping
    if args.json:
        print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()

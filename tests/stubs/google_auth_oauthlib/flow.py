"""Stub of google_auth_oauthlib.flow."""

from google.oauth2.credentials import Credentials


class InstalledAppFlow:
    """
    Stand-in for the interactive OAuth flow.

    run_local_server() returning instantly is itself useful: a test that
    reaches it when it should not have will pass suspiciously fast rather
    than hanging the suite.  test_cli.py asserts the non-interactive guard
    keeps us away from here.
    """

    @classmethod
    def from_client_secrets_file(cls, client_secrets_file, scopes):
        return cls()

    def run_local_server(self, port=0):
        return Credentials(source="interactive-flow")

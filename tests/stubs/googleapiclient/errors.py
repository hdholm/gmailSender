"""Stub of googleapiclient.errors."""


class Response(dict):
    """httplib2-style response: a header mapping with a .status."""

    def __init__(self, status, headers=None):
        super().__init__(headers or {})
        self.status = status


class HttpError(Exception):
    """Shaped like the real HttpError: .resp.status plus a Retry-After header."""

    def __init__(self, status, message="", headers=None, uri=None):
        self.resp = Response(status, headers)
        self.uri = uri or "https://gmail.googleapis.com/gmail/v1/users/me/messages/import"
        self.message = message
        super().__init__(
            f'<HttpError {status} when requesting {self.uri} returned "{message}">'
        )

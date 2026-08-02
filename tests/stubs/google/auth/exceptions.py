"""Stub of google.auth.exceptions."""


class GoogleAuthError(Exception):
    pass


class RefreshError(GoogleAuthError):
    pass


class TransportError(GoogleAuthError):
    pass

"""Stub of the httplib2 surface gmail_insert probes for transient errors."""


class HttpLib2Error(Exception):
    pass


class ServerNotFoundError(HttpLib2Error):
    pass

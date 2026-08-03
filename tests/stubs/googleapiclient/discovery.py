"""Stub of googleapiclient.discovery."""


class Service:
    """Records the credentials it was built from, for per-user assertions."""

    def __init__(self, credentials=None):
        self.credentials = credentials
        self.token_file = getattr(credentials, "source", None)


def build(serviceName=None, version=None, credentials=None, **kwargs):
    return Service(credentials)

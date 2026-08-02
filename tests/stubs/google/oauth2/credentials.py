"""Stub of google.oauth2.credentials."""


class Credentials:
    """
    Fake credentials that remember which token file produced them.

    `source` is what lets the multi-user tests prove that --flush used the
    right account's token for each spooled message.
    """

    def __init__(self, source=None, valid=True, expired=False,
                 refresh_token=None):
        self.source = source
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.refreshed = False

    @classmethod
    def from_authorized_user_file(cls, path, scopes):
        return cls(source=str(path))

    def refresh(self, request):
        self.refreshed = True
        self.valid = True
        self.expired = False

    def to_json(self):
        return "{}"

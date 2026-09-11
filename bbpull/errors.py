"""Typed errors so the CLI can exit with meaningful codes."""


class BBPullError(Exception):
    """Base class for all bbpull failures."""


class ConfigError(BBPullError):
    """Missing/invalid configuration (base URL, course id, credentials)."""


class LoginError(BBPullError):
    """Authentication did not produce a usable session."""


class ApiError(BBPullError):
    """A Learn REST endpoint returned an unexpected status."""

    def __init__(self, status, method, url, body=""):
        self.status = status
        self.method = method
        self.url = url
        self.body = (body or "")[:800]
        super().__init__(f"HTTP {status} {method} {url}\n{self.body}")

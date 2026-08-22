"""Helpers for safe logging of user-controlled data."""
from __future__ import annotations


def scrub(value: object) -> str:
    """Return ``value`` as a string safe to embed in a log entry.

    Strips carriage-return and line-feed characters so a user-controlled value
    cannot forge additional log lines (log injection, CWE-117).
    """
    return str(value).replace("\r", "").replace("\n", "")

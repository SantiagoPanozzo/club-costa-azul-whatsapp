"""Small helpers for useful logs that do not expose member identifiers."""

import hashlib


def log_reference(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()[:12]

"""One timezone-explicit format for human-readable CLI timestamps."""
from datetime import datetime, timezone


def utc(value):
    if value is None:
        return "从未"
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

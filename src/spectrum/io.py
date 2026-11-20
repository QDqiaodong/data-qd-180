"""Bounded JSON input and publish-once output; no business rules here."""
import json
import os
import tempfile
from pathlib import Path


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"non-finite JSON constant: {value}")


def load(path):
    raw = Path(path).read_bytes()
    if len(raw) > 8_000_000:
        raise ValueError("JSON exceeds 8 MB")
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    if not isinstance(value, dict):
        raise ValueError("root must be an object")
    return value


def publish(path, value):
    payload = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".publish-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        os.link(temp, target)  # atomic create, including rejection of dangling symlinks
    finally:
        os.unlink(temp)


def integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def unique(rows, label):
    if not isinstance(rows, list):
        raise ValueError(f"{label} must be an array")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{label} rows must be objects")
        key = text(row["id"], f"{label}.id")
        if key in result:
            raise ValueError(f"duplicate {label} id: {key}")
        result[key] = row
    return result

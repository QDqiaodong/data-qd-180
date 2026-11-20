"""A single local JSON ledger with process-safe atomic read-modify-write.

Every mutation takes an exclusive advisory lock on ``<ledger>.lock`` and replaces
the ledger atomically (write temp, fsync, rename, fsync directory).  Readers take
the same lock, so a command sees one complete committed state.  A crash either
leaves the previous file untouched or the complete new file.
"""
import fcntl
import json
import os
import tempfile
import uuid
from pathlib import Path

from .errors import WorkbenchError

FORMAT = "radio-workbench-ledger"
VERSION = 1
MAX_BYTES = 512 * 1024 * 1024


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"non-finite JSON constant: {value}")


class LedgerFile:
    """Context manager holding the ledger lock and the parsed ledger."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._fd = None
        self.data = None

    def __enter__(self):
        if not self.path.exists() or not self.path.is_file():
            raise WorkbenchError(
                "ledger_not_found", f"ledger is not a readable file: {self.path}"
            )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise WorkbenchError("ledger_io_error", str(exc)) from exc
        try:
            self.data = self._load()
        except BaseException:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._fd is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
        finally:
            self._fd = None
        return False

    # -- reading ---------------------------------------------------------
    def _load(self):
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise WorkbenchError("ledger_io_error", str(exc)) from exc
        if len(raw) > MAX_BYTES:
            raise WorkbenchError("ledger_corrupt", "ledger exceeds the supported size")
        try:
            data = json.loads(
                raw, object_pairs_hook=_pairs, parse_constant=_constant
            )
        except (ValueError, RecursionError) as exc:
            raise WorkbenchError("ledger_corrupt", f"unreadable ledger: {exc}") from exc
        _check_shape(data)
        return data

    # -- writing ---------------------------------------------------------
    def commit(self, data):
        _check_shape(data)
        payload = (
            json.dumps(
                data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        directory = self.path.parent
        _sweep_stale_temps(directory, self.path.name)
        fd, temp = tempfile.mkstemp(prefix=f".{self.path.name}.tmp-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            temp = None
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            raise WorkbenchError("ledger_io_error", str(exc)) from exc
        finally:
            if temp is not None:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
        self.data = data


def _sweep_stale_temps(directory, name):
    """Under the exclusive lock, drop temp files left by an interrupted commit."""
    prefix = f".{name}.tmp-"
    try:
        entries = os.listdir(directory)
    except OSError:
        return
    for entry in entries:
        if entry.startswith(prefix):
            try:
                os.unlink(os.path.join(directory, entry))
            except OSError:
                pass


def create_new(path, data):
    """Create a ledger only if no path exists; used by ``init``."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_name(target.name + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise WorkbenchError("ledger_io_error", str(exc)) from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if target.exists() or target.is_symlink():
            raise WorkbenchError(
                "ledger_exists", f"ledger already exists: {target}"
            )
        holder = LedgerFile(target)
        holder.data = data
        holder.commit(data)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def empty_ledger(stations):
    return {
        "format": FORMAT,
        "version": VERSION,
        "ledger_id": uuid.uuid4().hex,
        "stations": stations,
        "revisions": [],
        "proposals": {},
        "approvals": {},
    }


def _check_shape(data):
    if not isinstance(data, dict):
        raise WorkbenchError("ledger_corrupt", "ledger root must be an object")
    if data.get("format") != FORMAT or data.get("version") != VERSION:
        raise WorkbenchError("ledger_corrupt", "unsupported ledger format or version")
    if not isinstance(data.get("ledger_id"), str) or not data["ledger_id"]:
        raise WorkbenchError("ledger_corrupt", "ledger_id is missing")
    if not isinstance(data.get("stations"), list):
        raise WorkbenchError("ledger_corrupt", "stations are missing")
    revisions = data.get("revisions")
    if not isinstance(revisions, list) or not revisions:
        raise WorkbenchError("ledger_corrupt", "revisions are missing")
    for index, row in enumerate(revisions):
        if not isinstance(row, dict) or row.get("revision") != index:
            raise WorkbenchError("ledger_corrupt", "revision sequence is broken")
        if not isinstance(row.get("plan"), dict) or not isinstance(row.get("report"), dict):
            raise WorkbenchError("ledger_corrupt", "a revision is incomplete")
    proposals = data.get("proposals")
    if not isinstance(proposals, dict):
        raise WorkbenchError("ledger_corrupt", "proposals are missing")
    for key, proposal in proposals.items():
        if not isinstance(proposal, dict) or proposal.get("proposal_id") != key:
            raise WorkbenchError("ledger_corrupt", "a proposal is incomplete")
        for field, kind in (
            ("base_revision", int),
            ("upserts", list),
            ("remove_ids", list),
            ("content", dict),
            ("premises", dict),
        ):
            value = proposal.get(field)
            if not isinstance(value, kind) or (
                kind is int and type(value) is not int
            ):
                raise WorkbenchError("ledger_corrupt", "a proposal is incomplete")
        content = proposal["content"]
        if (
            type(content.get("base_revision")) is not int
            or not isinstance(content.get("upserts"), list)
            or not isinstance(content.get("remove_ids"), list)
        ):
            raise WorkbenchError("ledger_corrupt", "a proposal is incomplete")
        submitted = proposal.get("submitted")
        if submitted is not None and not (
            isinstance(submitted, dict)
            and type(submitted.get("revision")) is int
            and isinstance(submitted.get("result"), dict)
        ):
            raise WorkbenchError("ledger_corrupt", "a submission is incomplete")
    approvals = data.get("approvals")
    if not isinstance(approvals, dict):
        raise WorkbenchError("ledger_corrupt", "approvals are missing")
    for key, receivers in approvals.items():
        if key not in proposals or not isinstance(receivers, dict):
            raise WorkbenchError("ledger_corrupt", "approvals are incomplete")
        for identities in receivers.values():
            if not isinstance(identities, list) or not all(
                isinstance(item, str) for item in identities
            ):
                raise WorkbenchError("ledger_corrupt", "approvals are incomplete")

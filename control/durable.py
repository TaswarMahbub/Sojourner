"""Atomic whole-object replacement for persisted control state.

docs/contracts.md section 4, "Persisted status-gate state", write ordering:
a durable write is an atomic replacement of the whole object. Write a new
file, flush it, rename it over the old one, and flush the directory. The
write must complete before the value is used. A write that fails or cannot be
confirmed prevents the transmission or authorization that depended on it.

This helper raises on any failure. Callers treat an exception as "not
durable" and must not transmit or authorize.

Platform note: POSIX lets a directory be opened and fsynced, which makes the
rename itself durable. Windows does not allow opening a directory that way.
On Windows the rename is done with os.replace (MoveFileEx with
MOVEFILE_REPLACE_EXISTING), and the directory flush step is skipped. NTFS
journals the metadata change, but this call does not prove the rename reached
the disk. That gap is recorded here so the safety review sees it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def dumps_canonical(obj) -> str:
    """Compact, key-sorted JSON with NaN refused."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path, obj) -> None:
    """Replace `path` with `obj` as JSON: new file, fsync, rename, fsync directory.

    Raises OSError or ValueError on failure. On failure the previous complete
    file is left in place, or the temporary file is removed.
    """
    target = Path(path)
    text = dumps_canonical(obj)
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_directory(target.parent)


def read_text_or_none(path) -> str | None:
    """Return the file text, or None when the file does not exist.

    Other errors, such as permission or decoding failures, propagate.
    """
    target = Path(path)
    if not target.exists():
        return None
    return target.read_text(encoding="utf-8")

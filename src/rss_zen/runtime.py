"""Process-level runtime coordination for a single-host deployment."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from rss_zen.errors import AppError


@contextmanager
def single_instance_lock(database_path: Path) -> Iterator[None]:
    """Hold an advisory lock next to the SQLite database for one active service."""
    lock_path = database_path.with_suffix(f"{database_path.suffix}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        _lock(handle)
    except OSError as error:
        handle.close()
        raise AppError(
            "service_already_running",
            f"another RSS-Zen process owns the database lock: {lock_path}",
        ) from error
    try:
        yield
    finally:
        try:
            _unlock(handle)
        finally:
            handle.close()


def _lock(handle: object) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if handle.tell() == 0 and handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: object) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

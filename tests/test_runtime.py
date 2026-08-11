from __future__ import annotations

from pathlib import Path

import pytest

from rss_zen.errors import AppError
from rss_zen.runtime import single_instance_lock


def test_single_instance_lock_rejects_second_owner(tmp_path: Path) -> None:
    database_path = tmp_path / "rss-zen.sqlite3"

    with (
        single_instance_lock(database_path),
        pytest.raises(AppError, match="owns the database lock"),
        single_instance_lock(database_path),
    ):
        pass

    with single_instance_lock(database_path):
        pass

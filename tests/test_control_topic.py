from __future__ import annotations

import json

from typer.testing import CliRunner

from rss_zen.cli import app
from rss_zen.db import Database

RUNNER = CliRunner()


def test_topic_apply_and_lists_are_typed_and_audited(tmp_path) -> None:
    config = tmp_path / "rss-zen.toml"
    config.write_text(
        """
[database]
path="rss-zen.sqlite3"
[translation]
[[translation.providers]]
name="free"
kind="libretranslate"
endpoint="https://translate.example.test/translate"
""",
        encoding="utf-8",
    )
    Database(tmp_path / "rss-zen.sqlite3").initialize()
    applied = RUNNER.invoke(
        app,
        [
            "topic-apply",
            "--key",
            "indo-pacific",
            "--version",
            "1",
            "--name",
            "印太安全",
            "--keyword",
            "Taiwan",
            "--keyword",
            "AUKUS",
            "-c",
            str(config),
        ],
    )
    assert applied.exit_code == 0
    payload = json.loads(applied.stdout)
    assert payload["version"] == 1
    listed = RUNNER.invoke(app, ["topic-list", "-c", str(config)])
    topic = json.loads(listed.stdout)["topics"][0]
    assert topic["key"] == "indo-pacific"
    assert "selection" not in topic
    audit = json.loads(RUNNER.invoke(app, ["audit-list", "-c", str(config)]).stdout)
    assert audit["events"][0]["operation"] == "topic.apply"
    assert audit["events"][0]["metadata"]["keyword_count"] == 2

    conflict = RUNNER.invoke(
        app,
        [
            "topic-apply",
            "--key",
            "indo-pacific",
            "--version",
            "1",
            "--name",
            "Changed",
            "--keyword",
            "Taiwan",
            "-c",
            str(config),
        ],
    )
    assert conflict.exit_code == 1
    assert "topic_invalid" in conflict.stderr

    editions = RUNNER.invoke(app, ["edition-list", "-c", str(config)])
    assert json.loads(editions.stdout)["editions"] == []

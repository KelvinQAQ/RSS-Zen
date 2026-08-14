"""YAML and TOML configuration loading with environment-based secrets."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError

from rss_zen.errors import ConfigurationError
from rss_zen.models import (
    AnySearchSettings,
    AppConfig,
    FeedConfig,
    FeishuSettings,
    TranslationSettings,
)


def load_config(path: Path, *, environment: Mapping[str, str] | None = None) -> AppConfig:
    """Load one validated TOML or YAML configuration file."""
    raw_config = _read_config_file(path)
    try:
        config = AppConfig.model_validate(raw_config)
    except ValidationError as error:
        raise ConfigurationError(_format_validation_error(error), cause=error) from error
    return _resolve_secrets(config, environment or os.environ)


def _read_config_file(path: Path) -> Mapping[str, object]:
    if not path.is_file():
        raise ConfigurationError(f"configuration file does not exist: {path}")

    try:
        content = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() == ".toml":
            raw = tomllib.loads(content)
        elif path.suffix.lower() in {".yaml", ".yml"}:
            raw = yaml.safe_load(content)
        else:
            raise ConfigurationError("configuration format must be TOML, YAML, or YML")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"unable to read configuration: {error}", cause=error) from error

    if not isinstance(raw, Mapping):
        raise ConfigurationError("configuration root must be an object")
    return cast(Mapping[str, object], raw)


def _resolve_secrets(config: AppConfig, environment: Mapping[str, str]) -> AppConfig:
    providers = []
    for provider in config.translation.providers:
        api_key = _required_secret(provider.api_key_env, environment)
        providers.append(provider.model_copy(update={"api_key": api_key}))

    translation = TranslationSettings.model_validate(
        {**config.translation.model_dump(), "providers": providers}
    )
    feeds = [_resolve_feed_headers(feed, environment) for feed in config.feeds]
    anysearch = _resolve_anysearch_secret(config.anysearch, environment)
    feishu = _resolve_feishu_secrets(config.feishu, environment)
    return config.model_copy(
        update={
            "translation": translation,
            "feeds": feeds,
            "anysearch": anysearch,
            "feishu": feishu,
        }
    )


def _required_secret(name: str | None, environment: Mapping[str, str]) -> str | None:
    if name is None:
        return None
    value = environment.get(name)
    if not value:
        raise ConfigurationError(f"required environment variable is missing: {name}")
    return value


def _resolve_feed_headers(
    feed: FeedConfig, environment: Mapping[str, str]
) -> FeedConfig:
    """Resolve only explicitly declared sensitive feed headers from the environment."""
    resolved_headers = dict(feed.headers)
    for header_name, environment_name in feed.header_env.items():
        value = environment.get(environment_name)
        if not value:
            raise ConfigurationError(
                f"required environment variable is missing: {environment_name}"
            )
        resolved_headers[header_name] = value
    return feed.model_copy(update={"resolved_headers": resolved_headers})


def _resolve_anysearch_secret(
    settings: AnySearchSettings, environment: Mapping[str, str]
) -> AnySearchSettings:
    if settings.api_key_env is None:
        return settings
    return settings.model_copy(update={"api_key": environment.get(settings.api_key_env)})


def _resolve_feishu_secrets(
    settings: FeishuSettings, environment: Mapping[str, str]
) -> FeishuSettings:
    """Resolve optional delivery credentials so doctor can report missing values locally."""
    if not settings.enabled:
        return settings
    return settings.model_copy(
        update={
            "app_id": environment.get(settings.app_id_env or ""),
            "app_secret": environment.get(settings.app_secret_env or ""),
        }
    )


def _format_validation_error(error: ValidationError) -> str:
    details = error.errors(include_url=False)
    first = details[0]
    path = ".".join(str(part) for part in first["loc"])
    return f"invalid configuration at {path}: {first['msg']}"

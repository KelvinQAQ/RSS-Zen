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
from rss_zen.models import AnySearchSettings, AppConfig, TranslationSettings


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
        with path.open("rb") as handle:
            if path.suffix.lower() == ".toml":
                raw = tomllib.load(handle)
            elif path.suffix.lower() in {".yaml", ".yml"}:
                raw = yaml.safe_load(handle)
            else:
                raise ConfigurationError("configuration format must be TOML, YAML, or YML")
    except (OSError, tomllib.TOMLDecodeError, yaml.YAMLError) as error:
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
    anysearch = _resolve_anysearch_secret(config.anysearch, environment)
    return config.model_copy(update={"translation": translation, "anysearch": anysearch})


def _required_secret(name: str | None, environment: Mapping[str, str]) -> str | None:
    if name is None:
        return None
    value = environment.get(name)
    if not value:
        raise ConfigurationError(f"required environment variable is missing: {name}")
    return value


def _resolve_anysearch_secret(
    settings: AnySearchSettings, environment: Mapping[str, str]
) -> AnySearchSettings:
    if settings.api_key_env is None:
        return settings
    return settings.model_copy(update={"api_key": environment.get(settings.api_key_env)})


def _format_validation_error(error: ValidationError) -> str:
    details = error.errors(include_url=False)
    first = details[0]
    path = ".".join(str(part) for part in first["loc"])
    return f"invalid configuration at {path}: {first['msg']}"

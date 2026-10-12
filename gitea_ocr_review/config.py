"""Configuration loading and access-token resolution.

The config file is optional TOML, looked up at
``$XDG_CONFIG_HOME/gitea-ocr-review/config.toml`` (or
``~/.config/gitea-ocr-review/config.toml``) unless ``--config`` is given.

Example config::

    gitea_url = "https://gitea.example.com"
    token_env = "GITEA_TOKEN"      # recommended: keep the secret in the env
    # token   = "..."              # only for convenience; avoid committing this
    ocr_bin   = "ocr"
    reject_on = ["critical", "high"]

Token resolution precedence (highest first):

1. ``--token``
2. ``--token-file``
3. ``--token-stdin``
4. environment variable (``--token-env`` / ``token_env`` config / ``GITEA_TOKEN``)
5. ``token`` key in the config file
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import ConfigError

DEFAULT_TOKEN_ENV = "GITEA_TOKEN"
CONFIG_DIR_NAME = "gitea-ocr-review"
CONFIG_FILE_NAME = "config.toml"


def default_config_path() -> Path:
    """Return the platform-appropriate default config path."""

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def default_cache_dir() -> Path:
    """Return the default directory used to cache repository clones."""

    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / CONFIG_DIR_NAME


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load the TOML config file.

    Returns an empty mapping when the *default* path does not exist. An
    explicitly requested path that is missing is an error.
    """

    if path is None:
        path = default_config_path()
        if not path.exists():
            return {}
    else:
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")

    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - trivial
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    if not isinstance(data, dict):  # pragma: no cover - tomllib always yields a dict
        raise ConfigError(f"config file {path} must contain a TOML table")
    return data


def redact(token: str | None) -> str:
    """Return a log-safe representation of a secret."""

    if not token:
        return "<none>"
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]}"


def _read_token_file(path: Path) -> str:
    path = Path(path)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"cannot read token file {path}: {exc}") from exc
    if not value:
        raise ConfigError(f"token file {path} is empty")
    return value


def _env_token(names: Iterable[str], env: Mapping[str, str]) -> tuple[str | None, str | None]:
    for name in names:
        if not name:
            continue
        value = env.get(name)
        if value:
            return value.strip(), f"environment variable {name}"
    return None, None


def resolve_token(
    *,
    cli_token: str | None = None,
    token_file: Path | None = None,
    token_stdin: bool = False,
    token_env: str | None = None,
    config: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    stdin: Any | None = None,
) -> tuple[str, str]:
    """Resolve the Gitea access token and describe where it came from.

    Returns ``(token, source)`` where *source* never contains the secret.
    """

    config = config or {}
    env = os.environ if env is None else env
    stdin = sys.stdin if stdin is None else stdin

    if cli_token:
        return cli_token.strip(), "--token"

    if token_file:
        return _read_token_file(Path(token_file)), f"--token-file {token_file}"

    if token_stdin:
        value = stdin.readline().strip()
        if not value:
            raise ConfigError("no token received on stdin")
        return value, "--token-stdin"

    env_names: list[str] = []
    if token_env:
        env_names.append(token_env)
    if config.get("token_env"):
        env_names.append(str(config["token_env"]))
    if DEFAULT_TOKEN_ENV not in env_names:
        env_names.append(DEFAULT_TOKEN_ENV)

    value, source = _env_token(env_names, env)
    if value:
        return value, source or "environment"

    config_token = config.get("token")
    if config_token:
        return str(config_token).strip(), "config file"

    raise ConfigError(
        "no Gitea access token found",
        hint=(
            "pass --token, --token-file PATH or --token-stdin, "
            f"or set the {DEFAULT_TOKEN_ENV} environment variable"
        ),
    )

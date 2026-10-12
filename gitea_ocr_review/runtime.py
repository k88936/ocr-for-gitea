"""Runtime settings and logging shared by the CLI commands."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .config import default_cache_dir, redact, resolve_token
from .errors import ConfigError
from .gitea import GiteaClient
from .policy import DEFAULT_REJECT_ON, parse_reject_on

PROG = "gitea-ocr"


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def info(message: str, *, stream: TextIO | None = None) -> None:
    """Print a short prefixed status line (stderr by default)."""

    print(f"[{PROG}] {message}", file=stream or sys.stderr, flush=True)


class Logger:
    """Callable log sink.

    ``verbose`` gates debug-style messages for one-shot commands; ``always``
    forces every message through, which is what the long-running ``watch``
    command uses for stdout observability. ``timestamps`` prefixes each line.
    """

    def __init__(
        self,
        verbose: bool = False,
        *,
        stream: TextIO | None = None,
        always: bool = False,
        timestamps: bool = False,
        json_lines: bool = False,
    ) -> None:
        self.verbose = verbose
        self.always = always
        self.stream = stream
        self.timestamps = timestamps
        self.json_lines = json_lines
        # watch logs from several worker threads; keep whole lines from interleaving.
        self._lock = threading.Lock()

    def __call__(self, message: str) -> None:
        if not (self.verbose or self.always):
            return
        stream = self.stream if self.stream is not None else sys.stderr
        if self.json_lines:
            record = {
                "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "logger": PROG,
                "msg": message,
            }
            line = json.dumps(record)
        else:
            prefix = f"{_timestamp()} " if self.timestamps else ""
            line = f"{prefix}[{PROG}] {message}"
        with self._lock:
            print(line, file=stream, flush=True)


@dataclass
class Settings:
    gitea_url: str
    token: str
    token_source: str
    insecure: bool = False
    no_proxy: bool = False
    timeout: float = 30.0
    reject_on: tuple[str, ...] = DEFAULT_REJECT_ON
    ocr_bin: str = "ocr"
    ocr_timeout: float | None = None
    ocr_args: list[str] = field(default_factory=list)
    cache_dir: Path = field(default_factory=default_cache_dir)
    verbose: bool = False


def _cfg_value(config: dict[str, Any], key: str, default: Any = None) -> Any:
    value = config.get(key)
    return default if value is None else value


def resolve_settings(args: argparse.Namespace, config: dict[str, Any]) -> Settings:
    gitea_url = getattr(args, "gitea_url", None) or _cfg_value(config, "gitea_url") or ""
    token, token_source = resolve_token(
        cli_token=getattr(args, "token", None),
        token_file=getattr(args, "token_file", None),
        token_stdin=bool(getattr(args, "token_stdin", False)),
        token_env=getattr(args, "token_env", None),
        config=config,
    )

    reject_on_raw = (
        getattr(args, "reject_on", None)
        or _cfg_value(config, "reject_on")
        or DEFAULT_REJECT_ON
    )
    ocr_args = list(_cfg_value(config, "ocr_args", []) or [])
    ocr_args.extend(getattr(args, "ocr_arg", None) or [])

    return Settings(
        gitea_url=gitea_url,
        token=token,
        token_source=token_source,
        insecure=bool(getattr(args, "insecure", False) or _cfg_value(config, "insecure", False)),
        no_proxy=bool(getattr(args, "no_proxy", False) or _cfg_value(config, "no_proxy", False)),
        timeout=float(getattr(args, "timeout", None) or _cfg_value(config, "timeout", 30.0)),
        reject_on=parse_reject_on(reject_on_raw),
        ocr_bin=str(getattr(args, "ocr_bin", None) or _cfg_value(config, "ocr_bin", "ocr")),
        ocr_timeout=(
            getattr(args, "ocr_timeout", None)
            if getattr(args, "ocr_timeout", None) is not None
            else _cfg_value(config, "ocr_timeout")
        ),
        ocr_args=ocr_args,
        cache_dir=Path(
            getattr(args, "cache_dir", None)
            or _cfg_value(config, "cache_dir")
            or default_cache_dir()
        ),
        verbose=bool(getattr(args, "verbose", False) or _cfg_value(config, "verbose", False)),
    )


def make_client(settings: Settings) -> GiteaClient:
    if not settings.gitea_url:
        raise ConfigError(
            "gitea url is required",
            hint="pass --gitea-url https://gitea.example.com or set it in the config file",
        )
    return GiteaClient(
        settings.gitea_url,
        settings.token,
        insecure=settings.insecure,
        no_proxy=settings.no_proxy,
        timeout=settings.timeout,
    )

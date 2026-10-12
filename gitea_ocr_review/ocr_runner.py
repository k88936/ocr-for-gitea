"""Run the ``ocr`` CLI and parse its JSON review output."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import OcrError

Logger = Callable[[str], None]


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Finding:
    """A single review comment produced by OpenCodeReview."""

    path: str
    content: str
    severity: str = "unknown"
    category: str = ""
    start_line: int | None = None
    end_line: int | None = None
    suggestion_code: str = ""
    existing_code: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Finding":
        return cls(
            path=str(data.get("path") or ""),
            content=str(data.get("content") or "").strip(),
            severity=str(data.get("severity") or "unknown").strip().lower(),
            category=str(data.get("category") or "").strip().lower(),
            start_line=_as_int(data.get("start_line")),
            end_line=_as_int(data.get("end_line")),
            suggestion_code=str(data.get("suggestion_code") or ""),
            existing_code=str(data.get("existing_code") or ""),
        )

    @property
    def line(self) -> int | None:
        return self.start_line or self.end_line


@dataclass
class ReviewResult:
    """Parsed ``ocr review --format json`` payload."""

    status: str
    findings: list[Finding] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    provider: str | None = None
    model: str | None = None
    session_id: str | None = None
    message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)

    @property
    def comments_reported(self) -> int:
        return len(self.findings)


def parse_review_json(payload: dict[str, Any]) -> ReviewResult:
    """Build a :class:`ReviewResult` from an OCR JSON document."""

    llm = payload.get("llm") or {}
    findings = [Finding.from_dict(item) for item in payload.get("comments") or []]
    return ReviewResult(
        status=str(payload.get("status") or "unknown"),
        findings=findings,
        summary=dict(payload.get("summary") or {}),
        provider=llm.get("provider"),
        model=llm.get("model"),
        session_id=payload.get("session_id"),
        message=payload.get("message"),
        raw=payload,
    )


def build_command(
    ocr_bin: str,
    repo_dir: Path,
    base: str,
    head: str,
    output_path: Path,
    extra_args: Iterable[str] = (),
) -> list[str]:
    return [
        ocr_bin,
        "review",
        "--repo",
        str(repo_dir),
        "--from",
        base,
        "--to",
        head,
        "--format",
        "json",
        "--audience",
        "agent",
        "-o",
        str(output_path),
        *[str(a) for a in extra_args],
    ]


def run_ocr(
    repo_dir: Path,
    base: str,
    head: str,
    *,
    ocr_bin: str = "ocr",
    extra_args: Iterable[str] = (),
    timeout: float | None = None,
    log: Logger = lambda _msg: None,
) -> ReviewResult:
    """Execute ``ocr review`` and return the parsed result.

    Progress lines go to stderr by the tool's own design; we capture both
    streams so failures are explainable and stdout stays machine-clean.
    """

    repo_dir = Path(repo_dir)
    fd, tmp_name = tempfile.mkstemp(prefix="gitea-ocr-", suffix=".json")
    os.close(fd)
    output_path = Path(tmp_name)

    command = build_command(ocr_bin, repo_dir, base, head, output_path, extra_args)
    log("running: " + " ".join(command))

    try:
        proc = subprocess.run(
            command,
            cwd=str(repo_dir),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        output_path.unlink(missing_ok=True)
        raise OcrError(
            f"ocr executable not found: {ocr_bin!r}",
            hint="install alibaba/open-code-review and make sure `ocr` is on PATH, "
            "or pass --ocr-bin",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        output_path.unlink(missing_ok=True)
        raise OcrError(
            f"ocr review timed out after {timeout:g}s",
            hint="increase --ocr-timeout or review a smaller range",
        ) from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        output_path.unlink(missing_ok=True)
        raise OcrError(
            f"ocr review failed (exit {proc.returncode}): {detail[-800:]}",
            hint="run the same `ocr review` command manually to see the full log",
        )

    payload: dict[str, Any] | None = None
    if output_path.exists():
        raw = output_path.read_text(encoding="utf-8").strip()
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                output_path.unlink(missing_ok=True)
                raise OcrError(f"could not parse ocr JSON output: {exc}") from exc
    output_path.unlink(missing_ok=True)

    if payload is None:
        stdout = (proc.stdout or "").strip()
        if stdout:
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise OcrError(f"could not parse ocr stdout as JSON: {exc}") from exc
    if payload is None:
        raise OcrError("ocr produced no JSON output")
    if not isinstance(payload, dict):
        raise OcrError(
            f"ocr JSON output must be an object, got {type(payload).__name__}",
            hint="check the installed ocr version supports `--format json`",
        )

    result = parse_review_json(payload)
    result.command = command
    if result.status not in {"complete", "success", "ok"}:
        # Partial/budget-failed runs are still usable, but the operator should know.
        log(f"warning: ocr reported status={result.status!r}")
    return result

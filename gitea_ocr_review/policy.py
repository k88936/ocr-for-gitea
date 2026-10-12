"""Turn OCR findings into a Gitea review verdict, body and inline comments."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .ocr_runner import Finding

# Gitea review events (note: Gitea uses APPROVED, not GitHub's APPROVE).
EVENT_APPROVE = "APPROVED"
EVENT_REQUEST_CHANGES = "REQUEST_CHANGES"

SEVERITY_ORDER: dict[str, int] = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
    "unknown": 2,
}

DEFAULT_REJECT_ON = ("critical", "high")

_DECISION_ICON = {
    EVENT_APPROVE: "✅ Approved",
    EVENT_REQUEST_CHANGES: "⛔ Changes requested",
}


def normalize_severity(value: str | None) -> str:
    value = (value or "unknown").strip().lower()
    return value if value in SEVERITY_ORDER else "unknown"


def severity_rank(value: str | None) -> int:
    return SEVERITY_ORDER.get(normalize_severity(value), SEVERITY_ORDER["unknown"])


def parse_reject_on(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Parse ``"critical,high"`` or a list into a normalized tuple."""

    if value is None:
        return DEFAULT_REJECT_ON
    if isinstance(value, str):
        parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    else:
        parts = [str(p).strip().lower() for p in value if str(p).strip()]
    return tuple(parts) or DEFAULT_REJECT_ON


def blocking_findings(
    findings: Sequence[Finding],
    reject_on: Iterable[str] = DEFAULT_REJECT_ON,
) -> list[Finding]:
    """Findings whose severity is at least the lowest configured threshold."""

    thresholds = [severity_rank(s) for s in reject_on]
    minimum = min(thresholds) if thresholds else SEVERITY_ORDER["high"]
    return [f for f in findings if severity_rank(f.severity) >= minimum]


def summarize_findings(findings: Sequence[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        key = normalize_severity(finding.severity)
        counts[key] = counts.get(key, 0) + 1
    return counts


def decide_event(
    findings: Sequence[Finding],
    *,
    reject_on: Iterable[str] = DEFAULT_REJECT_ON,
) -> tuple[str, str]:
    """Return ``(gitea_event, human_readable_reason)``.

    The verdict is always derived from the findings: a finding at or above the
    lowest ``reject_on`` severity requests changes, everything else is approved.
    """

    blocking = blocking_findings(findings, reject_on)
    if blocking:
        return (
            EVENT_REQUEST_CHANGES,
            f"{len(blocking)} finding(s) at or above severity "
            f"{'/'.join(parse_reject_on(reject_on))}",
        )
    if findings:
        return (
            EVENT_APPROVE,
            f"no blocking findings (only {len(findings)} non-blocking finding(s))",
        )
    return EVENT_APPROVE, "no findings"


def _shorten(text: str, limit: int = 180) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _escape_table(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def build_review_body(
    *,
    pr: Mapping[str, Any],
    event: str,
    findings: Sequence[Finding],
    repo: str,
    base: str,
    head: str,
    result_meta: Mapping[str, Any] | None = None,
    inline_count: int = 0,
    max_rows: int = 100,
) -> str:
    """Render the markdown review summary (dry-run and ``--no-inline-comments``)."""

    result_meta = result_meta or {}
    number = pr.get("number")
    title = pr.get("title") or ""
    html_url = pr.get("html_url") or ""
    counts = summarize_findings(findings)

    header = _DECISION_ICON.get(event, event)
    lines: list[str] = []
    title_ref = f"#{number} {title}".strip()
    if html_url:
        lines.append(f"## {header} — [{title_ref}]({html_url})")
    else:
        lines.append(f"## {header} — {title_ref}")

    lines.append("")
    lines.append(f"**Decision:** `{event}`")
    lines.append(f"**Range:** `{base[:12]}..{head[:12]}` in `{repo}`")
    if result_meta.get("model"):
        provider = result_meta.get("provider") or "?"
        lines.append(f"**Reviewer model:** `{provider}/{result_meta['model']}`")
    files_reviewed = result_meta.get("files_reviewed")
    if files_reviewed is not None:
        lines.append(f"**Files reviewed:** {files_reviewed}")
    if counts:
        counts_text = ", ".join(f"{sev}: {n}" for sev, n in sorted(
            counts.items(), key=lambda item: -severity_rank(item[0])
        ))
        lines.append(f"**Findings:** {len(findings)} ({counts_text})")
    else:
        lines.append("**Findings:** 0")
    if inline_count:
        lines.append(f"**Inline comments posted:** {inline_count}")

    if findings:
        lines.append("")
        lines.append("### Findings")
        lines.append("")
        lines.append("| Severity | Location | Category | Finding |")
        lines.append("| --- | --- | --- | --- |")
        for finding in findings[:max_rows]:
            location = finding.path or "?"
            if finding.line:
                location += f":{finding.line}"
            lines.append(
                "| {sev} | `{loc}` | {cat} | {text} |".format(
                    sev=normalize_severity(finding.severity),
                    loc=_escape_table(location),
                    cat=_escape_table(finding.category or "-"),
                    text=_escape_table(_shorten(finding.content)),
                )
            )
        if len(findings) > max_rows:
            lines.append(f"| … | | | _{len(findings) - max_rows} more finding(s)_ |")

        with_suggestions = [f for f in findings if f.suggestion_code.strip()][:10]
        if with_suggestions:
            lines.append("")
            lines.append("<details><summary>Suggested changes</summary>")
            lines.append("")
            for finding in with_suggestions:
                location = finding.path or "?"
                if finding.line:
                    location += f":{finding.line}"
                lines.append(f"**`{location}`**")
                lines.append("")
                lines.append("```")
                lines.append(finding.suggestion_code.rstrip())
                lines.append("```")
                lines.append("")

    return "\n".join(lines)


def build_inline_comments(
    findings: Sequence[Finding],
    *,
    max_comments: int = 25,
    head_paths: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Map OCR findings to Gitea ``CreatePullReviewComment`` payloads.

    ``new_position`` is a 1-based line number in the *new* file, which is what
    OpenCodeReview reports as ``start_line``.
    """

    allowed = set(head_paths) if head_paths is not None else None
    comments: list[dict[str, Any]] = []
    for finding in findings:
        if len(comments) >= max_comments:
            break
        if not finding.path:
            continue
        if allowed is not None and finding.path not in allowed:
            continue
        line = finding.line
        if not line or line <= 0:
            continue

        # Severity, then OpenCodeReview's text, then its suggested change.
        text = finding.content.strip()
        suggestion = finding.suggestion_code
        if not text and not suggestion.strip():
            continue
        severity = normalize_severity(finding.severity)
        body_lines: list[str] = []
        if text:
            body_lines.append(f"**[{severity}]** {text}")
        else:
            body_lines.append(f"**[{severity}]**")
        if suggestion.strip():
            body_lines.append("")
            body_lines.append("Suggested change:")
            body_lines.append("")
            body_lines.append("```")
            body_lines.append(suggestion.rstrip())
            body_lines.append("```")
        comments.append(
            {
                "path": finding.path,
                "new_position": line,
                "old_position": 0,
                "body": "\n".join(body_lines),
            }
        )
    return comments

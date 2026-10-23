"""Render a pull request's discussion as Markdown for the OCR background."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def _login(item: Mapping[str, Any]) -> str:
    return str((item.get("user") or {}).get("login") or "unknown")


def _body(item: Mapping[str, Any]) -> str:
    return str(item.get("body") or "").strip()


def _created_at(item: Mapping[str, Any]) -> str:
    return str(item.get("created_at") or "")


def _review_comment_location(comment: Mapping[str, Any]) -> str:
    path = comment.get("path") or comment.get("original_path") or "?"
    line = comment.get("line") or comment.get("original_line")
    return f"`{path}:{line}`" if line else f"`{path}`"


def build_discussion_markdown(
    pr: Mapping[str, Any],
    *,
    issue_comments: Iterable[Mapping[str, Any]] = (),
    review_comments: Iterable[Mapping[str, Any]] = (),
) -> str:
    """Return a Markdown document with the PR description and every comment.

    The document is handed to ``ocr review --background-file`` so the reviewer
    sees the discussion that already happened on the pull request.
    """

    number = pr.get("number")
    title = str(pr.get("title") or "").strip()
    lines = [f"# Pull request #{number}: {title}".rstrip(": "), ""]

    author = (pr.get("user") or {}).get("login")
    if author:
        lines.append(f"**Author:** `{author}`")
    if pr.get("html_url"):
        lines.append(f"**URL:** {pr['html_url']}")
    lines.append("")

    description = str(pr.get("body") or "").strip()
    if description:
        lines.extend(["## Description", "", description, ""])

    entries: list[tuple[str, int, str, str]] = []
    sequence = 0
    for comment in issue_comments:
        body = _body(comment)
        if not body:
            continue
        entries.append(
            (_created_at(comment), sequence, f"comment by `{_login(comment)}`", body)
        )
        sequence += 1
    for comment in review_comments:
        body = _body(comment)
        if not body:
            continue
        heading = (
            f"review comment by `{_login(comment)}` "
            f"on {_review_comment_location(comment)}"
        )
        entries.append((_created_at(comment), sequence, heading, body))
        sequence += 1

    entries.sort(key=lambda entry: (entry[0], entry[1]))

    lines.extend(["## Comments", ""])
    if not entries:
        lines.extend(["_No comments yet._", ""])
    else:
        for timestamp, _, heading, body in entries:
            suffix = f" — {timestamp}" if timestamp else ""
            lines.extend([f"### {heading}{suffix}", "", body, ""])

    return "\n".join(lines).rstrip() + "\n"

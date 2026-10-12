"""The reusable pull-request review pipeline.

Both the one-shot ``review`` command and the long-running ``watch`` command use
:func:`perform_review`, so there is a single implementation of
"fetch PR -> checkout -> ocr -> verdict -> post".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import GiteaError
from .gitea import GiteaClient
from .gitrepo import (
    commit_exists,
    ensure_checkout,
    ensure_commits,
    list_files,
    merge_base,
    resolve_checkout_dir,
)
from .ocr_runner import ReviewResult, run_ocr
from .policy import (
    EVENT_REQUEST_CHANGES,
    build_inline_comments,
    build_review_body,
    decide_event,
)
from .runtime import Logger, Settings


@dataclass
class ReviewOptions:
    """Per-pull-request knobs (connection/LLM settings live in Settings)."""

    repo_dir: Path | None = None
    clone_url: str | None = None
    no_inline_comments: bool = False
    max_inline_comments: int = 25
    dry_run: bool = False
    force: bool = False


@dataclass
class ReviewOutcome:
    owner: str
    repo: str
    index: int
    pr: dict[str, Any]
    event: str
    reason: str
    body: str
    result: ReviewResult
    head_sha: str
    base_sha: str
    merge_base: str
    inline_comments: int
    review: dict[str, Any] | None = None
    dry_run: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "repository": f"{self.owner}/{self.repo}",
            "pull_request": self.index,
            "title": self.pr.get("title"),
            "event": self.event,
            "review_id": (self.review or {}).get("id"),
            "review_url": (self.review or {}).get("html_url"),
            "findings": len(self.result.findings),
            "inline_comments": self.inline_comments,
            "ocr_status": self.result.status,
            "ocr_session_id": self.result.session_id,
            "model": self.result.model,
            "provider": self.result.provider,
        }


def perform_review(
    client: GiteaClient,
    settings: Settings,
    owner: str,
    repo: str,
    index: int,
    options: ReviewOptions,
    log: Logger,
    *,
    pr: dict[str, Any] | None = None,
) -> ReviewOutcome:
    """Run the full review for one pull request.

    ``pr`` may be supplied by callers that already hold the pull-request
    payload (for example the ``watch`` command, whose ``list_pulls`` response
    already contains the base/head SHAs, ``merge_base`` and clone URL), so no
    extra ``GET /pulls/{index}`` is issued.

    Raises :class:`~gitea_ocr_review.errors.AppError` subclasses on failure.
    """

    if pr is None:
        pr = client.get_pull(owner, repo, index)
    if not pr:
        raise GiteaError("Gitea returned an empty pull request payload")

    state = pr.get("state")
    if state != "open" and not options.force:
        raise GiteaError(
            f"pull request {owner}/{repo}#{index} is not open (state={state})",
            hint="pass --force to review it anyway",
        )

    base_info = pr.get("base") or {}
    head_info = pr.get("head") or {}
    base_sha = base_info.get("sha")
    head_sha = head_info.get("sha")
    if not base_sha or not head_sha:
        raise GiteaError("pull request is missing base/head commit SHAs")

    clone_url = (
        options.clone_url
        or (base_info.get("repo") or {}).get("clone_url")
        or (head_info.get("repo") or {}).get("clone_url")
        or f"{settings.gitea_url.rstrip('/')}/{owner}/{repo}.git"
    )

    repo_dir = resolve_checkout_dir(
        options.repo_dir,
        settings.cache_dir,
        owner,
        repo,
        clone_url=clone_url,
    )
    remote = ensure_checkout(
        Path(repo_dir),
        clone_url,
        owner=owner,
        repo=repo,
        token=settings.token,
        no_proxy=settings.no_proxy,
        log=log,
    )
    ensure_commits(
        Path(repo_dir),
        base_sha=base_sha,
        head_sha=head_sha,
        pr_index=index,
        remote=remote,
        token=settings.token,
        no_proxy=settings.no_proxy,
        log=log,
    )

    diff_base = pr.get("merge_base") or ""
    if not commit_exists(repo_dir, diff_base):
        diff_base = merge_base(repo_dir, base_sha, head_sha) or ""
    if not diff_base:
        raise GiteaError(
            "could not determine the merge base for the pull request",
            hint="ensure the token can read the repository and the branches share history",
        )
    log(
        f"{owner}/{repo}#{index}: review range {diff_base[:12]}..{head_sha[:12]} "
        f"(PR base {base_sha[:12]})"
    )

    result = run_ocr(
        Path(repo_dir),
        diff_base,
        head_sha,
        ocr_bin=settings.ocr_bin,
        extra_args=settings.ocr_args,
        timeout=settings.ocr_timeout,
        log=log,
    )

    event, reason = decide_event(
        result.findings, reject_on=settings.reject_on
    )
    log(
        f"{owner}/{repo}#{index}: decision {event} "
        f"({reason}; {len(result.findings)} finding(s))"
    )

    inline_comments: list[dict[str, Any]] = []
    if not options.no_inline_comments:
        head_paths = list_files(repo_dir, head_sha)
        inline_comments = build_inline_comments(
            result.findings,
            max_comments=options.max_inline_comments,
            head_paths=head_paths,
        )

    body = build_review_body(
        pr=pr,
        event=event,
        findings=result.findings,
        repo=f"{owner}/{repo}",
        base=diff_base,
        head=head_sha,
        result_meta={
            "provider": result.provider,
            "model": result.model,
            "files_reviewed": result.summary.get("files_reviewed"),
        },
        inline_count=len(inline_comments),
    )

    review: dict[str, Any] | None = None
    if options.dry_run:
        log(f"{owner}/{repo}#{index}: dry run, not posting")
    else:
        log(
            f"{owner}/{repo}#{index}: posting {event} review"
            + (f" with {len(inline_comments)} inline comment(s)" if inline_comments else "")
        )
        # Code-side only by default: no summary comment on the pull request.
        # Gitea refuses REQUEST_CHANGES without a body, so use the short reason
        # there; --no-inline-comments explicitly asks for the full summary.
        if options.no_inline_comments:
            post_body = body
        elif event == EVENT_REQUEST_CHANGES:
            post_body = reason
        else:
            post_body = ""
        review = post_review(
            client,
            owner,
            repo,
            index,
            event=event,
            body=post_body,
            comments=inline_comments,
            commit_id=head_sha,
        )
        log(f"{owner}/{repo}#{index}: review posted (id={(review or {}).get('id')})")

    return ReviewOutcome(
        owner=owner,
        repo=repo,
        index=index,
        pr=pr,
        event=event,
        reason=reason,
        body=body,
        result=result,
        head_sha=head_sha,
        base_sha=base_sha,
        merge_base=diff_base,
        inline_comments=len(inline_comments),
        review=review,
        dry_run=options.dry_run,
    )


def post_review(
    client: GiteaClient,
    owner: str,
    repo: str,
    index: int,
    *,
    event: str,
    body: str,
    comments: list[dict[str, Any]],
    commit_id: str | None,
) -> dict[str, Any]:
    """Post the review, translating Gitea's self-approval error."""

    try:
        return client.create_review(
            owner,
            repo,
            index,
            event=event,
            body=body,
            comments=comments,
            commit_id=commit_id,
        )
    except GiteaError as exc:
        if "approve your own pull" in str(exc).lower():
            raise GiteaError(
                str(exc),
                status=exc.status,
                hint=(
                    "Gitea does not allow approving your own pull request; "
                    "use a token belonging to a different user"
                ),
            ) from exc
        raise

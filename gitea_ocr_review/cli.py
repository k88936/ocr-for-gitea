"""Command line interface for gitea-ocr-review."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import (
    default_config_path,
    load_config,
    redact,
)
from .errors import AppError, ConfigError, GiteaError
from .review import ReviewOptions, ReviewOutcome, perform_review
from .runtime import PROG, Logger, Settings, info, make_client, resolve_settings
from .state import ReviewState

# Re-exported for convenience and backwards compatibility.
__all__ = ["PROG", "Logger", "Settings", "build_parser", "main", "parse_target"]


# ---------------------------------------------------------------------------
# target parsing
# ---------------------------------------------------------------------------


def parse_target(
    *,
    target: str | None,
    number: str | None,
    repo_opt: str | None,
    pr_opt: str | None,
) -> tuple[str, str, int]:
    repo = repo_opt
    index: Any = pr_opt

    if target:
        if "#" in target:
            repo_part, _, num = target.partition("#")
            repo = repo or repo_part
            if index is None and num:
                index = num
        else:
            repo = repo or target
    if index is None and number:
        index = number

    if not repo or "/" not in repo:
        raise ConfigError(
            "pull request target is required",
            hint="use `review owner/repo#123` or `--repo owner/repo --pr 123`",
        )
    owner, name = _split_repo(repo)
    if index is None:
        raise ConfigError("pull request number is required", hint="add #123 or --pr 123")
    try:
        index_int = int(str(index).lstrip("#"))
    except ValueError as exc:
        raise ConfigError(f"invalid pull request number {index!r}") from exc
    return owner, name, index_int


def _split_repo(repo: str) -> tuple[str, str]:
    if "/" not in repo:
        raise ConfigError(f"invalid repository {repo!r}; expected owner/name")
    owner, _, name = repo.partition("/")
    owner, name = owner.strip(), name.strip()
    if not owner or not name:
        raise ConfigError(f"invalid repository {repo!r}; expected owner/name")
    return owner, name


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def command_review(args: argparse.Namespace, config: dict[str, Any]) -> int:
    settings = resolve_settings(args, config)
    log = Logger(settings.verbose)
    owner, repo, index = parse_target(
        target=args.target,
        number=args.number,
        repo_opt=args.repo_opt,
        pr_opt=args.pr_opt,
    )
    client = make_client(settings)
    log(
        f"gitea: {settings.gitea_url} (token from {settings.token_source}: "
        f"{redact(settings.token)})"
    )

    options = ReviewOptions(
        repo_dir=args.repo_dir,
        clone_url=args.clone_url,
        no_inline_comments=args.no_inline_comments,
        max_inline_comments=args.max_inline_comments,
        dry_run=args.dry_run,
        force=args.force,
    )
    info(f"fetching pull request {owner}/{repo}#{index}")
    outcome = perform_review(client, settings, owner, repo, index, options, log)
    _print_outcome(outcome, as_json=args.json)
    return 0


def _print_outcome(outcome: ReviewOutcome, *, as_json: bool) -> None:
    summary = outcome.summary()
    if outcome.dry_run:
        info("dry run: no review posted")
        if as_json:
            summary["body"] = outcome.body
            print(json.dumps(summary, indent=2))
        else:
            print(outcome.body)
        return
    info(
        f"{outcome.event} review posted for "
        f"{outcome.owner}/{outcome.repo}#{outcome.index}"
        + (f" ({outcome.inline_comments} inline comment(s))" if outcome.inline_comments else "")
    )
    if as_json:
        print(json.dumps(summary, indent=2))
    else:
        url = summary.get("review_url") or outcome.pr.get("html_url") or ""
        if url:
            print(url)


def command_watch(args: argparse.Namespace, config: dict[str, Any]) -> int:
    settings = resolve_settings(args, config)
    client = make_client(settings)

    # Watch logs go to stdout, timestamped, for observability.
    log = Logger(
        always=True,
        stream=sys.stdout,
        timestamps=not args.log_json,
        json_lines=args.log_json,
    )

    me = (client.get_authenticated_user() or {}).get("login")
    if not me:
        raise GiteaError("could not determine the authenticated Gitea user")

    repos = _resolve_watch_repos(client, args)
    state_path = (
        Path(args.state_file)
        if args.state_file
        else settings.cache_dir / "watch-state.json"
    )
    state = ReviewState(state_path, persist=not args.dry_run)
    options = ReviewOptions(
        no_inline_comments=args.no_inline_comments,
        max_inline_comments=args.max_inline_comments,
        dry_run=args.dry_run,
    )
    interval = max(0.0, float(args.interval))
    concurrency = max(1, int(getattr(args, "concurrency", 1) or 1))

    log(f"watching {len(repos)} repo(s) as {me} @ {settings.gitea_url}")
    log(f"repos: {', '.join(f'{owner}/{name}' for owner, name in repos)}")
    my_team_ids = _load_my_team_ids(client, log)
    log(
        f"interval={interval:g}s concurrency={concurrency} state={state_path}"
        + (" (dry-run: not persisted)" if args.dry_run else "")
    )

    cycles = 0
    try:
        while True:
            cycles += 1
            log(f"--- poll cycle #{cycles} ---")
            work = _collect_watch_work(client, state, me, my_team_ids, args, log, repos)
            reviewed_this_cycle = _run_watch_work(
                client, settings, state, options, log, work, concurrency
            )
            log(f"--- poll cycle #{cycles} complete ({reviewed_this_cycle} reviewed) ---")
            if args.once:
                break
            if interval > 0:
                log(f"sleeping {interval:g}s")
                time.sleep(interval)
    except KeyboardInterrupt:
        log("interrupted; stopping watch")
        state.save()
        return 130
    return 0


def _resolve_watch_repos(
    client: Any, args: argparse.Namespace
) -> list[tuple[str, str]]:
    repos: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(repo: str) -> None:
        key = _split_repo(repo)
        if key not in seen:
            seen.add(key)
            repos.append(key)

    for repo in getattr(args, "repos", None) or []:
        add(repo)

    if getattr(args, "org", None):
        for item in client.list_org_repos(args.org, limit=args.repo_limit):
            if item.get("full_name"):
                add(item["full_name"])

    if getattr(args, "user", None):
        for item in client.list_user_repos(args.user, limit=args.repo_limit):
            if item.get("full_name"):
                add(item["full_name"])

    if not repos:
        raise ConfigError(
            "no repositories to watch",
            hint="pass one or more owner/repo arguments, or --org ORG / --user USER",
        )
    return repos


def _load_my_team_ids(client: Any, log: Logger) -> set[int]:
    """Team ids the authenticated user belongs to, for team review requests."""

    try:
        teams = client.list_my_teams()
    except AppError as exc:
        log(f"could not list your teams ({exc}); ignoring team review requests")
        return set()
    return {t["id"] for t in teams if isinstance(t.get("id"), int)}


def _pending_review_request(
    client: Any,
    owner: str,
    name: str,
    index: int,
    me: str,
    my_team_ids: set[int],
    log: Logger,
) -> tuple[bool, bool]:
    """Open ``REQUEST_REVIEW`` rows, as ``(requested for me, for my team)``.

    Gitea keeps you in ``requested_reviewers`` on the list response even after
    you submitted your review (and even after an approval), so the reliable
    signal is a review row with ``state == "REQUEST_REVIEW"``. A team request
    is such a row carrying a ``team`` instead of a ``user``.
    """

    try:
        reviews = client.list_reviews(owner, name, index)
    except AppError as exc:
        log(f"{owner}/{name}#{index}: could not list reviews: {exc}")
        return False, False
    direct = False
    team = False
    for review in reviews:
        if review.get("state") != "REQUEST_REVIEW":
            continue
        if (review.get("user") or {}).get("login") == me:
            direct = True
        if (review.get("team") or {}).get("id") in my_team_ids:
            team = True
    return direct, team


def _collect_watch_work(
    client: Any,
    state: ReviewState,
    me: str,
    my_team_ids: set[int],
    args: argparse.Namespace,
    log: Logger,
    repos: list[tuple[str, str]],
) -> list[tuple[str, str, dict[str, Any]]]:
    """List each repository once and return the pending review requests.

    The list endpoint already carries everything the review needs — base and
    head SHAs, the merge base, the clone URL and the requests (direct
    ``requested_reviewers`` and ``requested_reviewers_teams``) — so no
    ``GET /pulls/{index}`` is issued per PR. Only pull requests that requested
    *me*, directly or through a team I belong to, and whose request is still
    open are returned.
    """

    work: list[tuple[str, str, dict[str, Any]]] = []
    for owner, name in repos:
        try:
            pulls = client.list_pulls(owner, name, state=args.pr_state, limit=args.limit)
        except AppError as exc:
            log(f"{owner}/{name}: listing failed: {exc}")
            continue

        log(f"{owner}/{name}: {len(pulls)} {args.pr_state} pull request(s)")
        for pr in pulls:
            index = pr.get("number")
            head_sha = (pr.get("head") or {}).get("sha") or ""
            author = (pr.get("user") or {}).get("login")
            raw_reviewers = pr.get("requested_reviewers") or []
            requested_users = {(u or {}).get("login") for u in raw_reviewers}
            listed_teams = {
                (t or {}).get("id")
                for t in (pr.get("requested_reviewers_teams") or [])
            }
            hinted = me in requested_users or bool(listed_teams & my_team_ids)

            if author == me and not args.include_own:
                log(f"{owner}/{name}#{index}: skip, authored by {me} (cannot self-approve)")
                continue
            if not args.re_review and state.was_reviewed(owner, name, index, head_sha):
                log(f"{owner}/{name}#{index}: skip, {head_sha[:12]} already reviewed")
                continue

            # The list payload cannot be trusted for team requests: Gitea 1.26
            # returns null for both requested_reviewers and
            # requested_reviewers_teams, and 1.22 hides the team behind a null
            # element. The open REQUEST_REVIEW row is the only reliable signal.
            pending_direct, pending_team = _pending_review_request(
                client, owner, name, index, me, my_team_ids, log
            )
            if pending_direct:
                via_team = False
            elif pending_team:
                via_team = True
            elif hinted:
                log(f"{owner}/{name}#{index}: skip, review already submitted")
                continue
            else:
                log(f"{owner}/{name}#{index}: skip, no review requested")
                continue

            if args.max_per_cycle and len(work) >= args.max_per_cycle:
                log(f"{owner}/{name}#{index}: --max-per-cycle reached, will resume next cycle")
                return work

            log(
                f"{owner}/{name}#{index}: review requested by {author} "
                f"({head_sha[:12]}{', team' if via_team else ''})"
            )
            work.append((owner, name, pr))
    return work


def _run_watch_work(
    client: Any,
    settings: Settings,
    state: ReviewState,
    options: ReviewOptions,
    log: Logger,
    work: list[tuple[str, str, dict[str, Any]]],
    concurrency: int,
) -> int:
    """Review the collected pull requests, optionally on a thread pool."""

    if not work:
        return 0

    if concurrency <= 1:
        return sum(
            1
            for owner, name, pr in work
            if _review_one(client, settings, state, options, log, owner, name, pr)
        )

    log(f"reviewing {len(work)} pull request(s) with {concurrency} worker(s)")
    reviewed = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                _review_one, client, settings, state, options, log, owner, name, pr
            ): (owner, name, pr.get("number"))
            for owner, name, pr in work
        }
        for future in as_completed(futures):
            owner, name, index = futures[future]
            try:
                if future.result():
                    reviewed += 1
            except Exception as exc:  # a worker failure must not kill the cycle
                log(f"{owner}/{name}#{index}: review failed: {exc}")
    return reviewed


def _review_one(
    client: Any,
    settings: Settings,
    state: ReviewState,
    options: ReviewOptions,
    log: Logger,
    owner: str,
    name: str,
    pr: dict[str, Any],
) -> bool:
    """Run (and record) one review. Safe to call from several threads."""

    index = pr.get("number")
    try:
        outcome = perform_review(
            client, settings, owner, name, index, options, log, pr=pr
        )
    except AppError as exc:
        log(f"{owner}/{name}#{index}: review failed: {exc}")
        return False

    state.mark(
        owner,
        name,
        index,
        head_sha=outcome.head_sha,
        event=outcome.event,
        review_id=(outcome.review or {}).get("id"),
        findings=len(outcome.result.findings),
        dry_run=outcome.dry_run,
    )
    state.save()
    log(
        f"{owner}/{name}#{index}: {outcome.event} "
        f"({outcome.reason}; {len(outcome.result.findings)} finding(s))"
    )
    return True


def command_list(args: argparse.Namespace, config: dict[str, Any]) -> int:
    settings = resolve_settings(args, config)
    log = Logger(settings.verbose)
    client = make_client(settings)
    repo = args.repo_opt or args.target
    if not repo or "/" not in repo:
        raise ConfigError("repository is required", hint="use `list owner/repo` or --repo owner/repo")
    owner, name = _split_repo(repo)
    log(f"listing {args.state} pull requests for {owner}/{name}")
    pulls = client.list_pulls(owner, name, state=args.state, limit=args.limit)

    if args.json:
        print(json.dumps(pulls, indent=2))
        return 0

    if not pulls:
        print(f"No {args.state} pull requests in {owner}/{name}")
        return 0
    print(f"{'#':>5}  {'state':<7} {'author':<16} title")
    for pull in pulls:
        author = (pull.get("user") or {}).get("login", "?")
        print(
            f"{pull.get('number'):>5}  {pull.get('state',''):<7} "
            f"{author[:16]:<16} {pull.get('title','')}"
        )
    return 0


def command_doctor(args: argparse.Namespace, config: dict[str, Any]) -> int:
    checks: list[tuple[str, bool, str]] = []

    config_path = Path(args.config) if args.config else default_config_path()
    if config_path.exists():
        checks.append(("config", True, str(config_path)))
    else:
        # An absent default config is fine; an explicitly requested one is not.
        checks.append(
            ("config", bool(getattr(args, "config", None)) is False, f"{config_path} (absent)")
        )

    try:
        settings = resolve_settings(args, config)
        checks.append(
            ("token", True, f"from {settings.token_source} ({redact(settings.token)})")
        )
    except AppError as exc:
        checks.append(("token", False, str(exc)))
        settings = None  # type: ignore[assignment]

    if settings is not None:
        try:
            client = make_client(settings)
            user = client.get_authenticated_user()
            checks.append(("gitea api", True, f"authenticated as {user.get('login')}"))
        except AppError as exc:
            checks.append(("gitea api", False, str(exc)))

    ocr_bin = getattr(args, "ocr_bin", None) or "ocr"
    path = shutil.which(ocr_bin)
    if not path:
        checks.append(("ocr", False, f"{ocr_bin!r} not found on PATH"))
    else:
        try:
            proc = subprocess.run(
                [ocr_bin, "version"], text=True, capture_output=True, timeout=60
            )
            output = (proc.stdout or proc.stderr or "").strip().splitlines()
            first = output[0] if output else f"exit {proc.returncode}"
            checks.append(("ocr", proc.returncode == 0, f"{path} ({first})"))
        except OSError as exc:
            checks.append(("ocr", False, str(exc)))

    ok = True
    for name, passed, detail in checks:
        mark = "OK  " if passed else "FAIL"
        if not passed:
            ok = False
        print(f"[{mark}] {name:<10} {detail}")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    default = argparse.SUPPRESS if suppress else None

    def add(*flags: str, **kwargs: Any) -> None:
        kwargs.setdefault("default", default)
        parser.add_argument(*flags, **kwargs)

    add("--config", type=Path, help="path to config.toml (default: ~/.config/gitea-ocr-review/config.toml)")
    add("--gitea-url", dest="gitea_url", help="Gitea base URL, e.g. https://gitea.example.com")
    add("--token", help="Gitea access token (prefer env/file; visible in ps)")
    add("--token-file", dest="token_file", type=Path, help="read the token from a file")
    add("--token-stdin", dest="token_stdin", action="store_true", help="read the token from stdin")
    add("--token-env", dest="token_env", help="environment variable holding the token")
    add("--insecure", action="store_true", help="skip TLS certificate verification")
    add("--no-proxy", dest="no_proxy", action="store_true", help="ignore HTTP(S) proxy env vars")
    add("--timeout", type=float, help="Gitea API timeout in seconds (default 30)")
    add("-v", "--verbose", action="store_true", help="verbose diagnostics")


def _add_review_knobs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--reject-on",
        dest="reject_on",
        help="comma-separated severities that trigger request-changes "
        "(default: critical,high); the lowest one is the threshold",
    )
    parser.add_argument("--cache-dir", dest="cache_dir", type=Path, help="where to cache clones")
    parser.add_argument("--ocr-bin", dest="ocr_bin", help="path to the ocr executable")
    parser.add_argument(
        "--ocr-arg", dest="ocr_arg", action="append", default=[],
        help="extra argument passed to `ocr review` (repeatable)",
    )
    parser.add_argument("--ocr-timeout", dest="ocr_timeout", type=float, help="ocr timeout in seconds")
    parser.add_argument("--no-inline-comments", action="store_true", help="do not attach line comments")
    parser.add_argument("--max-inline-comments", type=int, default=25, help="cap inline comments (default 25)")
    parser.add_argument("--dry-run", action="store_true", help="do not post anything to Gitea")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Review Gitea pull requests with alibaba/open-code-review and "
        "post approve / request-changes verdicts.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    _add_common(parser, suppress=False)

    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser(
        "review",
        aliases=["run"],
        help="review one pull request and post the verdict",
        description="Fetch a Gitea pull request, run `ocr review` on it and post "
        "an APPROVED or REQUEST_CHANGES review.",
    )
    _add_common(review, suppress=True)
    _add_review_knobs(review)
    review.add_argument("target", nargs="?", help="owner/repo or owner/repo#123")
    review.add_argument("number", nargs="?", help="pull request number")
    review.add_argument("--repo", dest="repo_opt", help="owner/repo")
    review.add_argument("--pr", dest="pr_opt", help="pull request number")
    review.add_argument("--repo-dir", dest="repo_dir", type=Path, help="existing local clone")
    review.add_argument("--clone-url", dest="clone_url", help="override the git clone URL")
    review.add_argument("--force", action="store_true", help="review even if the PR is not open")
    review.add_argument("--json", action="store_true", help="print a JSON summary")
    review.set_defaults(func=command_review)

    watch = sub.add_parser(
        "watch",
        aliases=["poll"],
        help="poll Gitea and automatically review new/changed pull requests",
        description="Continuously poll one or more repositories (or an org/user) for "
        "open pull requests and review any new head revision. Logs every action to stdout.",
    )
    _add_common(watch, suppress=True)
    _add_review_knobs(watch)
    watch.add_argument("repos", nargs="*", help="one or more owner/repo to watch")
    watch.add_argument("--org", help="watch every repository of this organization")
    watch.add_argument("--user", help="watch every repository of this user")
    watch.add_argument("--repo-limit", dest="repo_limit", type=int, default=50,
                       help="max repositories to expand from --org/--user (default 50)")
    watch.add_argument("--interval", type=float, default=60.0,
                       help="seconds between poll cycles (default 60)")
    watch.add_argument("--once", action="store_true", help="run one cycle and exit (cron-friendly)")
    watch.add_argument("--limit", type=int, default=50, help="max PRs fetched per repo per cycle")
    watch.add_argument("--pr-state", dest="pr_state", choices=["open", "closed", "all"], default="open",
                       help="which PRs to consider (default open)")
    watch.add_argument("--re-review", action="store_true",
                       help="ignore saved state and re-review even unchanged revisions")
    watch.add_argument("--include-own", action="store_true",
                       help="also process PRs authored by the token user")
    watch.add_argument("--max-per-cycle", dest="max_per_cycle", type=int, default=0,
                       help="cap reviews per cycle, 0 = unlimited")
    watch.add_argument("--concurrency", type=int, default=1, metavar="N",
                       help="review up to N pull requests in parallel (default 1)")
    watch.add_argument("--state-file", dest="state_file", type=Path,
                       help="path to the polling state JSON (default <cache>/watch-state.json)")
    watch.add_argument("--log-json", action="store_true",
                       help="emit stdout logs as JSON lines instead of text")
    watch.set_defaults(func=command_watch)

    listing = sub.add_parser("list", help="list pull requests in a repository")
    _add_common(listing, suppress=True)
    listing.add_argument("target", nargs="?", help="owner/repo")
    listing.add_argument("--repo", dest="repo_opt", help="owner/repo")
    listing.add_argument("--state", choices=["open", "closed", "all"], default="open")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=command_list)

    doctor = sub.add_parser("doctor", help="check config, token, Gitea and ocr")
    _add_common(doctor, suppress=True)
    doctor.add_argument("--ocr-bin", dest="ocr_bin")
    doctor.set_defaults(func=command_doctor)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = getattr(args, "command", None)
    if command in ("run",):
        args.command = "review"
    elif command in ("poll",):
        args.command = "watch"

    try:
        config = load_config(Path(args.config) if getattr(args, "config", None) else None)
        return int(args.func(args, config))
    except AppError as exc:
        print(exc.format(), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover
        print(f"\n[{PROG}] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

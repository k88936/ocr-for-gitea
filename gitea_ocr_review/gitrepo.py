"""Local git checkout helpers built on :mod:`git` (GitPython).

The review runs against a local clone, so we must guarantee that the base,
head and merge-base commits exist locally. The access token is injected
through git's ``GIT_CONFIG_*`` environment protocol so it never appears in
process arguments or in ``.git/config``.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable, Iterable

from git import Git, Repo
from git.exc import (
    GitCommandError,
    GitCommandNotFound,
    InvalidGitRepositoryError,
    NoSuchPathError,
)

from .errors import GitError

Logger = Callable[[str], None]

GITEA_REMOTE = "gitea"

_locks_guard = threading.Lock()
_repo_locks: dict[str, threading.Lock] = {}


def repo_lock(repo_dir: Path | str) -> threading.Lock:
    """Return a process-wide lock that serializes writes to one checkout.

    Concurrent workers may review different pull requests of the same
    repository, but git cannot safely run two fetches into the same
    ``.git`` directory at once, so callers guard mutating operations with this.
    """

    key = str(Path(repo_dir).resolve())
    with _locks_guard:
        lock = _repo_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _repo_locks[key] = lock
        return lock

_REPO_ERRORS = (InvalidGitRepositoryError, NoSuchPathError)
_GIT_ERRORS = (GitCommandError, GitCommandNotFound)


def _git_config(token: str | None, no_proxy: bool = False) -> dict[str, str]:
    """Build the ``GIT_CONFIG_*`` environment that injects secrets/proxy bits."""

    entries: list[tuple[str, str]] = []
    if token:
        entries.append(("http.extraHeader", f"Authorization: token {token}"))
    if no_proxy:
        entries.append(("http.proxy", ""))

    config: dict[str, str] = {}
    if entries:
        config["GIT_CONFIG_COUNT"] = str(len(entries))
        for i, (key, value) in enumerate(entries):
            config[f"GIT_CONFIG_KEY_{i}"] = key
            config[f"GIT_CONFIG_VALUE_{i}"] = value
    return config


def _git_env(token: str | None, no_proxy: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_ASKPASS", "true")
    env.update(_git_config(token, no_proxy))
    return env


class GitResult:
    """Minimal stand-in for :class:`subprocess.CompletedProcess`."""

    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.returncode == 0


def _git(cwd: Path | str | None, token: str | None, no_proxy: bool) -> Git:
    git = Git(str(cwd) if cwd else None)
    config = _git_config(token, no_proxy)
    if config:
        git.update_environment(**config)
    return git


def _repo(repo_dir: Path | str, token: str | None = None, no_proxy: bool = False) -> Repo:
    repo = Repo(str(repo_dir))
    config = _git_config(token, no_proxy)
    if config:
        repo.git.update_environment(**config)
    return repo


def run_git(
    args: Iterable[str],
    *,
    cwd: Path | str | None = None,
    token: str | None = None,
    no_proxy: bool = False,
    check: bool = True,
) -> GitResult:
    """Run git through GitPython, raising :class:`GitError` on failure."""

    argv = [str(a) for a in args]
    try:
        stdout = _git(cwd, token, no_proxy).execute(["git", *argv])
    except _GIT_ERRORS as exc:
        status = getattr(exc, "status", None) or 1
        stderr = getattr(exc, "stderr", None) or ""
        if not check:
            return GitResult(status, getattr(exc, "stdout", "") or "", stderr)
        safe = " ".join(a for a in argv if not a.startswith("Authorization"))
        detail = (stderr or getattr(exc, "stdout", "") or "").strip()
        raise GitError(
            f"git {safe} failed (exit {status}): {detail[-600:]}"
        ) from exc
    return GitResult(0, stdout, "")


def is_git_repo(path: Path | str) -> bool:
    return (Path(path) / ".git").exists()


def _remote_map(repo_dir: Path | str) -> dict[str, str]:
    try:
        repo = Repo(str(repo_dir))
    except _REPO_ERRORS:
        return {}
    remotes: dict[str, str] = {}
    for remote in repo.remotes:
        try:
            urls = list(remote.urls)
        except Exception:  # pragma: no cover - malformed remote config
            urls = []
        if urls:
            remotes.setdefault(remote.name, urls[0])
    return remotes


def remote_urls(repo_dir: Path | str) -> list[str]:
    """Return the configured fetch/push URLs of a checkout (deduplicated)."""

    urls: list[str] = []
    for url in _remote_map(repo_dir).values():
        if url not in urls:
            urls.append(url)
    return urls


def resolve_checkout_dir(
    repo_dir: Path | str | None,
    cache_dir: Path | str,
    owner: str,
    repo: str,
    *,
    clone_url: str | None = None,
) -> Path:
    """Choose the local checkout directory for ``owner/repo``.

    Priority:

    1. an explicit ``repo_dir`` / ``--repo-dir``;
    2. ``<cache_dir>/<owner>__<repo>`` if that checkout already exists;
    3. ``cache_dir`` itself when it is a git clone of the same repository, so
       ``--cache-dir`` may point straight at an existing working copy;
    4. ``<cache_dir>/<owner>__<repo>`` (which will then be cloned).
    """

    if repo_dir:
        return Path(repo_dir)

    cache = Path(cache_dir)
    nested = cache / f"{owner}__{repo}"
    if (nested / ".git").exists():
        return nested

    if (cache / ".git").exists():
        if not remote_urls(cache) or matching_remote(
            cache, clone_url, owner=owner, repo=repo
        ):
            return cache

    return nested


def commit_exists(repo_dir: Path | str, sha: str | None) -> bool:
    if not sha:
        return False
    try:
        _repo(repo_dir).git.cat_file("-e", f"{sha}^{{commit}}")
    except _GIT_ERRORS + _REPO_ERRORS:
        return False
    return True


def resolve_commit(repo_dir: Path | str, ref: str) -> str:
    try:
        return _repo(repo_dir).git.rev_parse("--verify", f"{ref}^{{commit}}").strip()
    except _GIT_ERRORS + _REPO_ERRORS as exc:
        raise GitError(f"cannot resolve {ref}") from exc


def merge_base(repo_dir: Path | str, left: str, right: str) -> str | None:
    try:
        out = _repo(repo_dir).git.merge_base(left, right)
    except _GIT_ERRORS + _REPO_ERRORS:
        return None
    return out.strip() or None


def _normalize_url(url: str) -> str:
    return url.strip().lower().rstrip("/").removesuffix(".git")


def matching_remote(
    repo_dir: Path | str,
    clone_url: str | None = None,
    *,
    owner: str | None = None,
    repo: str | None = None,
) -> str | None:
    """Return an existing remote that already points at the target repository."""

    remotes = _remote_map(repo_dir)
    if not remotes:
        return None

    target = _normalize_url(clone_url) if clone_url else None
    for name, url in remotes.items():
        if target and _normalize_url(url) == target:
            return name

    slug = f"{owner}/{repo}".lower() if owner and repo else None
    if slug:
        for name, url in remotes.items():
            if slug in url.lower():
                return name
    return None


def ensure_checkout(
    repo_dir: Path,
    clone_url: str,
    *,
    owner: str | None = None,
    repo: str | None = None,
    token: str | None = None,
    no_proxy: bool = False,
    log: Logger = lambda _msg: None,
) -> str:
    """Clone into ``repo_dir`` or reuse it and return the remote to fetch from.

    If a remote already points at ``clone_url`` (such as the ``origin`` created
    by ``git clone``, or an existing checkout's remote) it is reused, so no
    extra remote is written into the repository.
    """

    repo_dir = Path(repo_dir)
    with repo_lock(repo_dir):
        if is_git_repo(repo_dir):
            log(f"using existing checkout {repo_dir}")
        else:
            if repo_dir.exists() and any(repo_dir.iterdir()):
                raise GitError(
                    f"{repo_dir} exists but is not a git repository; "
                    "pass --repo-dir with a git clone or choose an empty cache dir"
                )
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            log(f"cloning {clone_url} -> {repo_dir}")
            try:
                Repo.clone_from(clone_url, str(repo_dir), env=_git_env(token, no_proxy))
            except _GIT_ERRORS as exc:
                detail = (getattr(exc, "stderr", "") or "").strip()
                raise GitError(f"git clone {clone_url} failed: {detail[-600:]}") from exc

        remote = matching_remote(repo_dir, clone_url, owner=owner, repo=repo)
        if remote:
            log(f"using remote '{remote}' -> {clone_url}")
            return remote

        _set_remote(repo_dir, GITEA_REMOTE, clone_url, token=token, no_proxy=no_proxy)
        log(f"adding remote '{GITEA_REMOTE}' -> {clone_url}")
        return GITEA_REMOTE


def _set_remote(
    repo_dir: Path,
    name: str,
    url: str,
    *,
    token: str | None = None,
    no_proxy: bool = False,
) -> None:
    repo = _repo(repo_dir, token, no_proxy)
    if name in {remote.name for remote in repo.remotes}:
        repo.git.remote("set-url", name, url)
    else:
        repo.git.remote("add", name, url)


def fetch_heads(
    repo_dir: Path,
    *,
    remote: str = GITEA_REMOTE,
    token: str | None = None,
    no_proxy: bool = False,
    log: Logger = lambda _msg: None,
) -> None:
    log(f"fetching branches from {remote}")
    try:
        _repo(repo_dir, token, no_proxy).git.fetch(
            "--prune", remote, f"+refs/heads/*:refs/remotes/{remote}/*"
        )
    except _GIT_ERRORS as exc:
        detail = (getattr(exc, "stderr", "") or "").strip()
        raise GitError(f"git fetch {remote} failed: {detail[-600:]}") from exc


def fetch_pr_head(
    repo_dir: Path,
    index: int,
    *,
    remote: str = GITEA_REMOTE,
    token: str | None = None,
    no_proxy: bool = False,
    log: Logger = lambda _msg: None,
) -> str | None:
    """Fetch ``refs/pull/<index>/head`` (works for fork PRs)."""

    log(f"fetching refs/pull/{index}/head")
    ref = int(index)
    try:
        _repo(repo_dir, token, no_proxy).git.fetch(
            remote,
            f"+refs/pull/{ref}/head:refs/remotes/{remote}/pr/{ref}",
        )
    except _GIT_ERRORS as exc:
        detail = (getattr(exc, "stderr", "") or "").strip()
        raise GitError(f"git fetch refs/pull/{ref}/head failed: {detail[-600:]}") from exc
    return resolve_commit_or_none(repo_dir, f"refs/remotes/{remote}/pr/{ref}")


def list_files(repo_dir: Path | str, sha: str) -> list[str]:
    """Return the repository-relative file paths that exist at ``sha``."""

    try:
        out = _repo(repo_dir).git.ls_tree("-r", "--name-only", sha)
    except _GIT_ERRORS + _REPO_ERRORS:
        return []
    return [line for line in out.splitlines() if line.strip()]


def resolve_commit_or_none(repo_dir: Path | str, ref: str) -> str | None:
    try:
        return resolve_commit(repo_dir, ref)
    except GitError:
        return None


def fetch_commit(
    repo_dir: Path,
    sha: str,
    *,
    remote: str = GITEA_REMOTE,
    token: str | None = None,
    no_proxy: bool = False,
) -> bool:
    try:
        _repo(repo_dir, token, no_proxy).git.fetch(remote, sha)
    except _GIT_ERRORS + _REPO_ERRORS:
        return False
    return commit_exists(repo_dir, sha)


def ensure_commits(
    repo_dir: Path,
    *,
    base_sha: str,
    head_sha: str,
    pr_index: int | None = None,
    remote: str = GITEA_REMOTE,
    token: str | None = None,
    no_proxy: bool = False,
    log: Logger = lambda _msg: None,
) -> None:
    """Make sure ``base_sha`` and ``head_sha`` are present locally."""

    with repo_lock(repo_dir):
        missing = [
            sha for sha in (base_sha, head_sha) if sha and not commit_exists(repo_dir, sha)
        ]
        if not missing:
            return

        fetch_heads(repo_dir, remote=remote, token=token, no_proxy=no_proxy, log=log)

        for sha in list(missing):
            if not commit_exists(repo_dir, sha):
                fetch_commit(repo_dir, sha, remote=remote, token=token, no_proxy=no_proxy)

        if head_sha and not commit_exists(repo_dir, head_sha) and pr_index is not None:
            fetch_pr_head(
                repo_dir,
                pr_index,
                remote=remote,
                token=token,
                no_proxy=no_proxy,
                log=log,
            )

        still_missing = [
            sha for sha in (base_sha, head_sha) if sha and not commit_exists(repo_dir, sha)
        ]
        if still_missing:
            raise GitError(
                "could not fetch required commit(s) from Gitea: "
                + ", ".join(still_missing)
                + "; make sure the token can read the repository"
            )

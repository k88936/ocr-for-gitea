"""Persistent state for the polling ``watch`` command.

The state maps ``owner/repo#number`` to the last head commit that was reviewed,
so a long-running process (or a cron-driven ``watch --once``) does not review
the same revision twice and automatically re-reviews a PR when new commits land.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ReviewState:
    """JSON-backed record of reviewed revisions.

    With ``persist=False`` the state is kept in memory only, which the
    ``watch --dry-run`` mode uses to avoid re-reviewing within one process
    without polluting the on-disk state.
    """

    VERSION = 1

    def __init__(self, path: Path, *, persist: bool = True) -> None:
        self.path = Path(path)
        self.persist = persist
        # Reentrant so query helpers can call each other; guards every access
        # because ``watch`` may update the state from several worker threads.
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        pulls = data.get("pulls")
        if not isinstance(pulls, dict):
            pulls = {}
        return {"version": self.VERSION, "pulls": pulls}

    def save(self) -> None:
        if not self.persist:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._data, indent=2, sort_keys=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=self.path.name + ".", dir=str(self.path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self.path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise

    # -- queries -----------------------------------------------------------

    @staticmethod
    def key(owner: str, repo: str, index: int) -> str:
        return f"{owner}/{repo}#{int(index)}"

    def get(self, owner: str, repo: str, index: int) -> dict[str, Any] | None:
        with self._lock:
            entry = self._data["pulls"].get(self.key(owner, repo, index))
            return entry if isinstance(entry, dict) else None

    def head_sha(self, owner: str, repo: str, index: int) -> str | None:
        entry = self.get(owner, repo, index)
        return entry.get("head_sha") if entry else None

    def was_reviewed(self, owner: str, repo: str, index: int, head_sha: str) -> bool:
        return bool(head_sha) and self.head_sha(owner, repo, index) == head_sha

    # -- updates -----------------------------------------------------------

    def mark(
        self,
        owner: str,
        repo: str,
        index: int,
        *,
        head_sha: str,
        event: str,
        review_id: int | None = None,
        findings: int = 0,
        dry_run: bool = False,
    ) -> None:
        with self._lock:
            self._data["pulls"][self.key(owner, repo, index)] = {
                "head_sha": head_sha,
                "event": event,
                "review_id": review_id,
                "findings": findings,
                "dry_run": dry_run,
                "reviewed_at": _utc_now(),
            }

    def remove(self, owner: str, repo: str, index: int) -> None:
        with self._lock:
            self._data["pulls"].pop(self.key(owner, repo, index), None)

    def all_entries(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data["pulls"])

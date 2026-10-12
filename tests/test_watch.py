import contextlib
import io
import json
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from gitea_ocr_review.cli import main
from gitea_ocr_review.gitrepo import resolve_commit, run_git
from gitea_ocr_review.state import ReviewState

OCR_JSON = {
    "status": "complete",
    "llm": {"provider": "test", "model": "fake"},
    "summary": {"files_reviewed": 1, "comments": 1},
    "comments": [
        {
            "path": "calc.py",
            "content": "add() subtracts",
            "suggestion_code": "    return a + b",
            "start_line": 2,
            "end_line": 2,
            "category": "bug",
            "severity": "critical",
        }
    ],
    "session_id": "s",
}


class _State:
    def __init__(self):
        self.requests = []
        self.responses = {}


class _Handler(BaseHTTPRequestHandler):
    state: _State

    def log_message(self, *_args):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else None
        self.state.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "body": json.loads(body) if body else None,
            }
        )
        status, payload = self.state.responses.get((self.command, self.path), (200, {}))
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _handle
    do_POST = _handle


class WatchTests(unittest.TestCase):
    me = "reviewbot"

    def setUp(self):
        self.state = _State()
        _Handler.state = self.state
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.repo = self._make_repo()
        self.base_sha = resolve_commit(self.repo, "main")
        self.heads = {
            "feature1": resolve_commit(self.repo, "feature1"),
            "feature2": resolve_commit(self.repo, "feature2"),
            "feature3": resolve_commit(self.repo, "feature3"),
        }
        self.ocr = self._fake_ocr()
        self.config = self.tmp_path / "config.toml"
        self.config.write_text("")
        self.state_file = self.tmp_path / "watch-state.json"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    # -- fixtures ----------------------------------------------------------

    def _make_repo(self) -> Path:
        repo = self.tmp_path / "src"
        repo.mkdir()
        run_git(["init", "-q", "-b", "main"], cwd=repo)
        run_git(["config", "user.email", "t@t.com"], cwd=repo)
        run_git(["config", "user.name", "test"], cwd=repo)
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        run_git(["add", "-A"], cwd=repo)
        run_git(["commit", "-qm", "init"], cwd=repo)
        for branch in ("feature1", "feature2", "feature3"):
            run_git(["checkout", "-qB", branch, "main"], cwd=repo)
            (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
            run_git(["add", "-A"], cwd=repo)
            run_git(["commit", "-qm", f"bug on {branch}"], cwd=repo)
        run_git(["checkout", "-q", "main"], cwd=repo)
        return repo

    def _fake_ocr(self) -> Path:
        script = self.tmp_path / "fake_ocr"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "out = sys.argv[sys.argv.index('-o') + 1]\n"
            f"open(out, 'w').write({json.dumps(OCR_JSON)!r})\n"
        )
        script.chmod(0o755)
        return script

    def _pull(
        self,
        number,
        branch,
        author="alice",
        requested=None,
        pending=True,
        teams=None,
        pending_team=None,
        team_marker=False,
        null_reviewers=False,
    ):
        return {
            "number": number,
            "branch": branch,
            "author": author,
            # Default: this PR asks the token's user for a review.
            "requested": [self.me] if requested is None else list(requested),
            "pending": pending,
            "teams": list(teams or []),
            "pending_team": pending_team,
            "team_marker": team_marker,
            "null_reviewers": null_reviewers,
            "title": f"PR {number}",
        }

    def _list_view(self, pull):
        # Mirrors the real Gitea list payload, which already carries the base
        # and head SHAs, the merge base, the clone URL and the review requests.
        reviewers = [{"login": u} for u in pull["requested"]]
        teams = [{"id": t, "name": f"team{t}"} for t in pull["teams"]]
        if pull.get("team_marker"):
            # Gitea 1.22 hides a team request as a null reviewer, with no teams.
            reviewers.append(None)
            teams = None
        if pull.get("null_reviewers"):
            # Gitea 1.26 returns null for both fields on a team-only request.
            reviewers = None
            teams = None
        return {
            "number": pull["number"],
            "state": "open",
            "title": pull["title"],
            "user": {"login": pull["author"]},
            "html_url": f"{self.base_url}/o/r/pulls/{pull['number']}",
            "merge_base": self.base_sha,
            "head": {"sha": self.heads[pull["branch"]]},
            "base": {"sha": self.base_sha, "repo": {"clone_url": str(self.repo)}},
            "requested_reviewers": reviewers,
            "requested_reviewers_teams": teams,
        }

    def _configure(self, pulls, *, teams=None):
        self.state.responses.clear()
        self.state.responses[("GET", "/api/v1/user")] = (200, {"login": self.me})
        self.state.responses[
            ("GET", "/api/v1/user/teams?limit=50&page=1")
        ] = (200, list(teams or []))
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/pulls?state=open&limit=50&page=1")
        ] = (200, [self._list_view(p) for p in pulls])
        for pull in pulls:
            number = pull["number"]
            # Only an open request for me or my team yields a REQUEST_REVIEW row.
            rows: list[dict] = []
            if pull.get("pending", True):
                if pull.get("pending_team") is not None:
                    rows = [
                        {"id": 1, "state": "REQUEST_REVIEW",
                         "team": {"id": pull["pending_team"]}}
                    ]
                elif self.me in pull["requested"]:
                    rows = [
                        {"id": 1, "state": "REQUEST_REVIEW",
                         "user": {"login": self.me}}
                    ]
            self.state.responses[
                ("GET", f"/api/v1/repos/o/r/pulls/{number}/reviews")
            ] = (200, rows)
            # Deliberately no per-PR GET response: any such request would get
            # an empty payload and fail, proving watch uses the list payload.
            self.state.responses[("POST", f"/api/v1/repos/o/r/pulls/{number}/reviews")] = (
                200,
                {"id": 100 + number, "state": "REQUEST_CHANGES", "html_url": "http://x"},
            )

    def _run_watch(self, *extra):
        argv = [
            "watch",
            "o/r",
            "--once",
            "--gitea-url",
            self.base_url,
            "--token",
            "test-token",
            "--no-proxy",
            "--config",
            str(self.config),
            "--cache-dir",
            str(self.tmp_path / "cache"),
            "--ocr-bin",
            str(self.ocr),
            "--state-file",
            str(self.state_file),
            *extra,
        ]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(list(argv))
        return code, buf.getvalue()

    def _posts(self):
        return [r for r in self.state.requests if r["method"] == "POST"]

    def _pr_gets(self):
        """Requests that fetch a single pull request (which watch must avoid)."""

        return [
            r
            for r in self.state.requests
            if r["method"] == "GET"
            and re.fullmatch(r"/api/v1/repos/o/r/pulls/\d+", r["path"])
        ]

    # -- tests -------------------------------------------------------------

    def test_reviews_new_pr_and_skips_own(self):
        self._configure(
            [self._pull(1, "feature1", "alice"), self._pull(2, "feature2", self.me)]
        )
        code, out = self._run_watch()
        self.assertEqual(code, 0)
        posts = self._posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["path"], "/api/v1/repos/o/r/pulls/1/reviews")
        self.assertEqual(posts[0]["body"]["event"], "REQUEST_CHANGES")
        self.assertIn("skip, authored by reviewbot", out)
        self.assertIn("REQUEST_CHANGES", out)

        state = ReviewState(self.state_file)
        self.assertEqual(state.head_sha("o", "r", 1), self.heads["feature1"])
        self.assertIsNone(state.head_sha("o", "r", 2))

    def test_second_cycle_skips_already_reviewed(self):
        self._configure([self._pull(1, "feature1", "alice")])
        self._run_watch()
        self.assertEqual(len(self._posts()), 1)

        self.state.requests.clear()
        code, out = self._run_watch()
        self.assertEqual(code, 0)
        self.assertEqual(self._posts(), [])
        self.assertIn("already reviewed", out)

    def test_re_review_forces_another_post(self):
        self._configure([self._pull(1, "feature1", "alice")])
        self._run_watch()
        self.state.requests.clear()
        code, _out = self._run_watch("--re-review")
        self.assertEqual(code, 0)
        self.assertEqual(len(self._posts()), 1)

    def test_skips_prs_without_a_review_request(self):
        self._configure(
            [
                self._pull(1, "feature1", "alice"),           # requests me
                self._pull(2, "feature2", "alice", requested=()),  # does not
            ]
        )
        code, out = self._run_watch()
        self.assertEqual(code, 0)
        self.assertEqual(
            [p["path"] for p in self._posts()],
            ["/api/v1/repos/o/r/pulls/1/reviews"],
        )
        self.assertIn("no review requested", out)

    def test_skips_review_request_that_was_already_submitted(self):
        # Listed as a requested reviewer, but the REQUEST_REVIEW row is gone
        # (Gitea keeps stale requested_reviewers), so it must not be reviewed.
        self._configure([self._pull(1, "feature1", "alice", pending=False)])
        code, out = self._run_watch()
        self.assertEqual(code, 0)
        self.assertEqual(self._posts(), [])
        self.assertIn("review already submitted", out)

    def test_reviews_pr_requested_through_a_team(self):
        self._configure(
            [
                self._pull(
                    1, "feature1", "alice", requested=(), teams=[7], pending_team=7
                )
            ],
            teams=[{"id": 7, "name": "Developers"}],
        )
        code, out = self._run_watch()
        self.assertEqual(code, 0)
        self.assertEqual(len(self._posts()), 1)
        self.assertIn("review requested by alice", out)
        self.assertIn(", team)", out)

    def test_reviews_pr_with_team_request_hidden_as_null_reviewer(self):
        # Gitea 1.22 lists no team reviewers: a team request appears as a null
        # entry in requested_reviewers, and the team itself shows up in /reviews.
        self._configure(
            [
                self._pull(
                    1,
                    "feature1",
                    "alice",
                    requested=(),
                    teams=[7],
                    pending_team=7,
                    team_marker=True,
                )
            ],
            teams=[{"id": 7, "name": "Developers"}],
        )
        code, out = self._run_watch()
        self.assertEqual(code, 0)
        self.assertEqual(len(self._posts()), 1)
        self.assertIn(", team)", out)

    def test_reviews_team_request_when_list_nulls_both_fields(self):
        # Gitea 1.26 regression: a team-only request yields null for both
        # requested_reviewers and requested_reviewers_teams, so /reviews is the
        # only way to see the Developers team request.
        self._configure(
            [
                self._pull(
                    1,
                    "feature1",
                    "Fiorina",
                    requested=(),
                    teams=[7],
                    pending_team=7,
                    null_reviewers=True,
                )
            ],
            teams=[{"id": 7, "name": "Developers"}],
        )
        code, out = self._run_watch()
        self.assertEqual(code, 0)
        self.assertEqual(len(self._posts()), 1)
        self.assertIn("review requested by Fiorina", out)
        self.assertIn(", team)", out)

    def test_skips_team_request_for_a_team_i_am_not_in(self):
        self._configure(
            [self._pull(1, "feature1", "alice", requested=(), teams=[9])],
            teams=[{"id": 7, "name": "Developers"}],
        )
        code, out = self._run_watch()
        self.assertEqual(code, 0)
        self.assertEqual(self._posts(), [])
        self.assertIn("no review requested", out)

    def test_skips_team_request_that_was_already_submitted(self):
        self._configure(
            [
                self._pull(
                    1,
                    "feature1",
                    "alice",
                    requested=(),
                    teams=[7],
                    pending=False,
                    pending_team=7,
                )
            ],
            teams=[{"id": 7, "name": "Developers"}],
        )
        code, out = self._run_watch()
        self.assertEqual(code, 0)
        self.assertEqual(self._posts(), [])
        self.assertIn("review already submitted", out)

    def test_max_per_cycle_bounds_work(self):
        self._configure(
            [
                self._pull(1, "feature1", "alice"),
                self._pull(2, "feature2", "alice"),
                self._pull(3, "feature3", "alice"),
            ]
        )
        code, out = self._run_watch("--max-per-cycle", "1")
        self.assertEqual(code, 0)
        self.assertEqual(len(self._posts()), 1)
        self.assertIn("--max-per-cycle reached", out)

    def test_dry_run_posts_nothing_and_persists_nothing(self):
        self._configure([self._pull(1, "feature1", "alice")])
        code, out = self._run_watch("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self._posts(), [])
        self.assertFalse(self.state_file.exists())
        self.assertIn("dry run", out)

    def test_log_json_is_machine_readable(self):
        self._configure([self._pull(1, "feature1", "alice")])
        code, out = self._run_watch("--log-json")
        self.assertEqual(code, 0)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertTrue(lines)
        for line in lines:
            record = json.loads(line)
            self.assertIn("ts", record)
            self.assertIn("msg", record)
        self.assertTrue(any("poll cycle" in json.loads(line)["msg"] for line in lines))

    def test_watch_uses_list_payload_without_per_pr_get(self):
        self._configure([self._pull(1, "feature1", "alice")])
        code, _out = self._run_watch()
        self.assertEqual(code, 0)
        self.assertEqual(len(self._posts()), 1)
        # The list response already had base/head/merge_base/clone_url.
        self.assertEqual(self._pr_gets(), [])

    def test_concurrency_reviews_each_pull_request_once(self):
        self._configure(
            [
                self._pull(1, "feature1", "alice"),
                self._pull(2, "feature2", "alice"),
                self._pull(3, "feature3", "alice"),
            ]
        )
        code, _out = self._run_watch("--concurrency", "3")
        self.assertEqual(code, 0)
        self.assertEqual(
            sorted(p["path"] for p in self._posts()),
            [
                "/api/v1/repos/o/r/pulls/1/reviews",
                "/api/v1/repos/o/r/pulls/2/reviews",
                "/api/v1/repos/o/r/pulls/3/reviews",
            ],
        )
        state = ReviewState(self.state_file)
        self.assertEqual(len(state.all_entries()), 3)
        for number, branch in ((1, "feature1"), (2, "feature2"), (3, "feature3")):
            self.assertEqual(state.head_sha("o", "r", number), self.heads[branch])

    def test_concurrency_actually_overlaps_reviews(self):
        self._configure(
            [
                self._pull(1, "feature1", "alice"),
                self._pull(2, "feature2", "alice"),
                self._pull(3, "feature3", "alice"),
            ]
        )
        barrier = threading.Barrier(3)
        seen: list[int] = []
        lock = threading.Lock()

        def fake_perform_review(
            client, settings, owner, repo, index, options, log, *, pr=None
        ):
            with lock:
                seen.append(index)
            # Blocks until all three workers are inside at once. With a serial
            # implementation this times out and every review fails.
            barrier.wait(timeout=10)
            outcome = mock.Mock()
            outcome.head_sha = (pr["head"] or {}).get("sha") or ""
            outcome.event = "APPROVED"
            outcome.reason = "concurrent test"
            outcome.review = {"id": index}
            outcome.result.findings = []
            outcome.dry_run = False
            return outcome

        with mock.patch(
            "gitea_ocr_review.cli.perform_review", side_effect=fake_perform_review
        ):
            code, _out = self._run_watch("--concurrency", "3")

        self.assertEqual(code, 0)
        self.assertEqual(sorted(seen), [1, 2, 3])
        self.assertEqual(len(ReviewState(self.state_file).all_entries()), 3)


if __name__ == "__main__":
    unittest.main()

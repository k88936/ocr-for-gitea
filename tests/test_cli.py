import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gitea_ocr_review.cli import build_parser, main, parse_target
from gitea_ocr_review.errors import ConfigError
from gitea_ocr_review.gitrepo import resolve_commit, run_git


class ParseTargetTests(unittest.TestCase):
    def test_hash_form(self):
        self.assertEqual(
            parse_target(target="o/r#7", number=None, repo_opt=None, pr_opt=None),
            ("o", "r", 7),
        )

    def test_two_positionals(self):
        self.assertEqual(
            parse_target(target="o/r", number="7", repo_opt=None, pr_opt=None),
            ("o", "r", 7),
        )

    def test_flags(self):
        self.assertEqual(
            parse_target(target=None, number=None, repo_opt="o/r", pr_opt="7"),
            ("o", "r", 7),
        )

    def test_missing_number(self):
        with self.assertRaises(ConfigError):
            parse_target(target="o/r", number=None, repo_opt=None, pr_opt=None)

    def test_missing_repo(self):
        with self.assertRaises(ConfigError):
            parse_target(target="not-a-repo", number="1", repo_opt=None, pr_opt=None)


class ParserMergingTests(unittest.TestCase):
    def test_global_option_before_subcommand(self):
        args = build_parser().parse_args(["--gitea-url", "http://a", "review", "o/r#1"])
        self.assertEqual(args.gitea_url, "http://a")

    def test_global_option_after_subcommand(self):
        args = build_parser().parse_args(["review", "o/r#1", "--gitea-url", "http://b"])
        self.assertEqual(args.gitea_url, "http://b")

    def test_alias(self):
        args = build_parser().parse_args(["run", "o/r#1"])
        self.assertEqual(args.command, "run")

    def test_version_exits(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main(["--version"])
        self.assertEqual(ctx.exception.code, 0)


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


FAKE_OCR_JSON = {
    "status": "complete",
    "llm": {"provider": "test", "model": "fake-model"},
    "summary": {"files_reviewed": 1, "comments": 1},
    "comments": [
        {
            "path": "calc.py",
            "content": "add() subtracts",
            "suggestion_code": "    return a + b",
            "existing_code": "    return a - b",
            "start_line": 2,
            "end_line": 2,
            "category": "bug",
            "severity": "critical",
        }
    ],
    "session_id": "s1",
}


class FullPipelineTests(unittest.TestCase):
    """Exercise fetch-PR -> clone -> run ocr -> post review with fakes."""

    def setUp(self):
        self.state = _State()
        _Handler.state = self.state
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.repo = self._make_repo()
        self.base_sha = resolve_commit(self.repo, "main")
        self.head_sha = resolve_commit(self.repo, "feature")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _make_repo(self) -> Path:
        repo = self.tmp_path / "src"
        repo.mkdir()
        run_git(["init", "-q", "-b", "main"], cwd=repo)
        run_git(["config", "user.email", "t@t.com"], cwd=repo)
        run_git(["config", "user.name", "test"], cwd=repo)
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        run_git(["add", "-A"], cwd=repo)
        run_git(["commit", "-qm", "init"], cwd=repo)
        run_git(["checkout", "-qb", "feature"], cwd=repo)
        (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        run_git(["add", "-A"], cwd=repo)
        run_git(["commit", "-qm", "bug"], cwd=repo)
        return repo

    def _fake_ocr(self) -> Path:
        self.ocr_argv = self.tmp_path / "ocr-argv.json"
        self.ocr_background = self.tmp_path / "ocr-background.md"
        script = self.tmp_path / "fake_ocr"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, shutil, sys\n"
            "out = sys.argv[sys.argv.index('-o') + 1]\n"
            f"open(out, 'w').write({json.dumps(FAKE_OCR_JSON)!r})\n"
            f"open({str(self.ocr_argv)!r}, 'w').write(json.dumps(sys.argv))\n"
            "if '--background-file' in sys.argv:\n"
            "    index = sys.argv.index('--background-file')\n"
            f"    shutil.copyfile(sys.argv[index + 1], {str(self.ocr_background)!r})\n"
        )
        script.chmod(0o755)
        return script

    def _empty_config(self) -> Path:
        path = self.tmp_path / "config.toml"
        path.write_text("")
        return path

    def test_review_posts_request_changes(self):
        self.state.responses[("GET", "/api/v1/repos/o/r/pulls/1")] = (
            200,
            {
                "number": 1,
                "title": "Change add",
                "state": "open",
                "html_url": f"{self.base_url}/o/r/pulls/1",
                "merge_base": self.base_sha,
                "base": {"sha": self.base_sha, "repo": {"clone_url": str(self.repo)}},
                "head": {"sha": self.head_sha},
            },
        )
        self.state.responses[("POST", "/api/v1/repos/o/r/pulls/1/reviews")] = (
            200,
            {"id": 42, "state": "REQUEST_CHANGES", "html_url": "http://x/42"},
        )
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/issues/1/comments?limit=50&page=1")
        ] = (
            200,
            [
                {
                    "body": "please keep add() adding",
                    "user": {"login": "bob"},
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
        )
        # Gitea has no global review-comment endpoint, so 404 here must fall
        # back to listing each review's comments.
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/pulls/1/comments?limit=50&page=1")
        ] = (404, {"message": "not found"})
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/pulls/1/reviews?limit=50&page=1")
        ] = (200, [{"id": 5, "state": "COMMENT"}, {"id": 6, "state": "APPROVED"}])
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/pulls/1/reviews/5/comments?limit=50&page=1")
        ] = (
            200,
            [
                {
                    "body": "this subtraction is wrong",
                    "user": {"login": "carol"},
                    "created_at": "2026-01-02T00:00:00Z",
                    "path": "calc.py",
                    "line": 2,
                }
            ],
        )
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/pulls/1/reviews/6/comments?limit=50&page=1")
        ] = (200, [])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(
                [
                    "review",
                    "o/r#1",
                    "--gitea-url",
                    self.base_url,
                    "--token",
                    "test-token",
                    "--no-proxy",
                    "--config",
                    str(self._empty_config()),
                    "--cache-dir",
                    str(self.tmp_path / "cache"),
                    "--ocr-bin",
                    str(self._fake_ocr()),
                    "--json",
                    "-v",
                ]
            )
        self.assertEqual(code, 0)
        posted = [r for r in self.state.requests if r["method"] == "POST"]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["body"]["event"], "REQUEST_CHANGES")
        self.assertEqual(posted[0]["body"]["comments"][0]["path"], "calc.py")
        inline = posted[0]["body"]["comments"][0]["body"]
        self.assertIn("add() subtracts", inline)
        self.assertIn("Suggested change", inline)
        self.assertIn(
            "![](https://github.com/TreapGoGo/deepseek-whale-girl/blob/main/"
            "site/assets/gallery/meme-",
            inline,
        )
        self.assertEqual(posted[0]["body"]["commit_id"], self.head_sha)
        # Gitea requires a body for request-changes; it is the short reason.
        self.assertIn("finding(s)", posted[0]["body"]["body"])
        summary = json.loads(buf.getvalue())
        self.assertEqual(summary["event"], "REQUEST_CHANGES")

        # The whole conversation is handed to ocr as a Markdown background.
        argv = json.loads(self.ocr_argv.read_text())
        background_index = argv.index("--background-file")
        self.assertTrue(argv[background_index + 1].endswith(".md"))
        background = self.ocr_background.read_text()
        self.assertIn("please keep add() adding", background)
        self.assertIn("this subtraction is wrong", background)

    def test_dry_run_posts_nothing(self):
        self.state.responses[("GET", "/api/v1/repos/o/r/pulls/2")] = (
            200,
            {
                "number": 2,
                "title": "Clean",
                "state": "open",
                "merge_base": self.base_sha,
                "base": {"sha": self.base_sha, "repo": {"clone_url": str(self.repo)}},
                "head": {"sha": self.head_sha},
            },
        )
        # The discussion is optional context: a failed lookup must not block review.
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/issues/2/comments?limit=50&page=1")
        ] = (500, {"message": "forbidden"})
        self.state.responses[
            ("GET", "/api/v1/repos/o/r/pulls/2/comments?limit=50&page=1")
        ] = (500, {"message": "forbidden"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(
                [
                    "review",
                    "o/r#2",
                    "--gitea-url",
                    self.base_url,
                    "--token",
                    "test-token",
                    "--no-proxy",
                    "--config",
                    str(self._empty_config()),
                    "--cache-dir",
                    str(self.tmp_path / "cache"),
                    "--ocr-bin",
                    str(self._fake_ocr()),
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0)
        self.assertFalse([r for r in self.state.requests if r["method"] == "POST"])
        self.assertIn("REQUEST_CHANGES", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

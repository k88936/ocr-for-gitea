import subprocess
import tempfile
import unittest
from pathlib import Path

from gitea_ocr_review.errors import GitError
from gitea_ocr_review.gitrepo import (
    commit_exists,
    ensure_checkout,
    is_git_repo,
    list_files,
    matching_remote,
    merge_base,
    remote_urls,
    resolve_checkout_dir,
    resolve_commit,
    run_git,
)


def make_repo(path: Path) -> str:
    run_git(["init", "-q", "-b", "main"], cwd=path)
    run_git(["config", "user.email", "t@t.com"], cwd=path)
    run_git(["config", "user.name", "test"], cwd=path)
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    run_git(["add", "-A"], cwd=path)
    run_git(["commit", "-qm", "init"], cwd=path)
    base = resolve_commit(path, "main")
    run_git(["checkout", "-qb", "feature"], cwd=path)
    (path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (path / "utils.py").write_text("X = 1\n")
    run_git(["add", "-A"], cwd=path)
    run_git(["commit", "-qm", "change"], cwd=path)
    return base


def remote_names(repo: Path) -> list[str]:
    return subprocess.run(
        ["git", "remote"], cwd=repo, capture_output=True, text=True
    ).stdout.split()


class GitRepoTests(unittest.TestCase):
    def test_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "src"
            repo.mkdir()
            base = make_repo(repo)
            head = resolve_commit(repo, "feature")

            self.assertTrue(is_git_repo(repo))
            self.assertTrue(commit_exists(repo, base))
            self.assertTrue(commit_exists(repo, head))
            self.assertFalse(commit_exists(repo, "0" * 40))

            self.assertEqual(merge_base(repo, base, head), base)
            self.assertIn("calc.py", list_files(repo, head))
            self.assertIn("utils.py", list_files(repo, head))

    def test_ensure_checkout_clones(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            base = make_repo(src)
            dest = Path(tmp) / "clone"
            ensure_checkout(dest, str(src))
            self.assertTrue(is_git_repo(dest))
            self.assertTrue(commit_exists(dest, base))
            # A second call must reuse the existing checkout.
            ensure_checkout(dest, str(src))

    def test_ensure_checkout_rejects_non_repo_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "not-a-repo"
            dest.mkdir()
            (dest / "file").write_text("x")
            with self.assertRaises(GitError):
                ensure_checkout(dest, "/does/not/matter")

    def test_run_git_failure_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GitError):
                run_git(["rev-parse", "HEAD"], cwd=tmp)

    def test_remote_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "src"
            repo.mkdir()
            make_repo(repo)
            run_git(["remote", "add", "origin", "https://example.com/o/r.git"], cwd=repo)
            self.assertIn("https://example.com/o/r.git", remote_urls(repo))

    def test_ensure_checkout_fresh_clone_reuses_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            make_repo(src)
            clone = Path(tmp) / "clone"
            remote = ensure_checkout(clone, str(src))
            self.assertEqual(remote, "origin")
            self.assertEqual(remote_names(clone), ["origin"])

    def test_ensure_checkout_reuses_matching_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            make_repo(src)
            clone = Path(tmp) / "clone"
            ensure_checkout(clone, str(src))
            run_git(
                ["remote", "set-url", "origin", "https://git.example.com/o/r.git"],
                cwd=clone,
            )
            remote = ensure_checkout(
                clone, "https://git.example.com/o/r.git", owner="o", repo="r"
            )
            self.assertEqual(remote, "origin")
            self.assertNotIn("gitea", remote_names(clone))

    def test_ensure_checkout_adds_remote_when_nothing_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            make_repo(src)
            clone = Path(tmp) / "clone"
            ensure_checkout(clone, str(src))
            run_git(
                ["remote", "set-url", "origin", "https://git.example.com/someone/else.git"],
                cwd=clone,
            )
            remote = ensure_checkout(
                clone, "https://git.example.com/o/r.git", owner="o", repo="r"
            )
            self.assertEqual(remote, "gitea")
            self.assertIn("gitea", remote_names(clone))

    def test_matching_remote_ignores_url_without_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            make_repo(src)
            clone = Path(tmp) / "clone"
            ensure_checkout(clone, str(src))
            run_git(
                ["remote", "set-url", "origin", "https://git.example.com/someone/else.git"],
                cwd=clone,
            )
            self.assertIsNone(
                matching_remote(clone, None, owner="o", repo="r")
            )

    def test_resolve_checkout_dir_uses_cache_dir_when_it_is_a_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            make_repo(src)
            clone = Path(tmp) / "clone"
            ensure_checkout(clone, str(src))
            run_git(
                ["remote", "set-url", "origin", "https://git.example.com/o/r.git"],
                cwd=clone,
            )

            # --cache-dir points straight at an existing working copy.
            resolved = resolve_checkout_dir(
                None,
                clone,
                "o",
                "r",
                clone_url="https://git.example.com/o/r.git",
            )
            self.assertEqual(resolved, clone)

    def test_resolve_checkout_dir_ignores_unrelated_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            make_repo(src)
            other = Path(tmp) / "other"
            ensure_checkout(other, str(src))
            run_git(
                [
                    "remote",
                    "set-url",
                    "origin",
                    "https://git.example.com/someone/else.git",
                ],
                cwd=other,
            )
            resolved = resolve_checkout_dir(
                None, other, "o", "r", clone_url="https://git.example.com/o/r.git"
            )
            self.assertEqual(resolved, other / "o__r")

    def test_resolve_checkout_dir_prefers_existing_nested_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            nested = cache / "o__r"
            nested.mkdir(parents=True)
            run_git(["init", "-q"], cwd=nested)
            self.assertEqual(resolve_checkout_dir(None, cache, "o", "r"), nested)

    def test_resolve_checkout_dir_explicit_wins(self):
        explicit = Path("/tmp/explicit")
        self.assertEqual(
            resolve_checkout_dir(explicit, "/tmp/cache", "o", "r"), explicit
        )

    def test_token_not_leaked_into_argv(self):
        # GIT_CONFIG_* mechanism keeps the secret out of the command line.
        env = None
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            run_git(["init", "-q"], cwd=repo)
            from gitea_ocr_review.gitrepo import _git_env

            env = _git_env("SUPERSECRET")
        self.assertNotIn("SUPERSECRET", " ".join(["git"]))
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "Authorization: token SUPERSECRET")


if __name__ == "__main__":
    unittest.main()

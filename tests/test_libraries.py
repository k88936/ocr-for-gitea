"""Prove that the hand-rolled layers were replaced by the mature packages.

These tests fail if someone reintroduces ``urllib.request`` / ``subprocess`` or
bypasses httpx / GitPython.
"""

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from git import Repo

from gitea_ocr_review import gitea, gitrepo


class LibraryUsageTests(unittest.TestCase):
    def test_gitea_module_builds_on_httpx_not_urllib(self):
        source = inspect.getsource(gitea)
        self.assertIn("import httpx", source)
        self.assertNotIn("urllib.request", source)
        self.assertNotIn("http.client", source)

    def test_gitrepo_module_builds_on_gitpython_not_subprocess(self):
        source = inspect.getsource(gitrepo)
        self.assertIn("from git import", source)
        self.assertNotIn("import subprocess", source)

    def test_client_holds_an_httpx_client(self):
        client = gitea.GiteaClient("http://example.invalid", "t", no_proxy=True)
        self.addCleanup(client.close)
        self.assertIsInstance(client._client, httpx.Client)

    def test_api_calls_go_through_httpx_request(self):
        client = gitea.GiteaClient("http://example.invalid", "t", no_proxy=True)
        self.addCleanup(client.close)
        response = httpx.Response(
            200,
            json={"login": "k88936"},
            request=httpx.Request("GET", "http://example.invalid/api/v1/user"),
        )
        with mock.patch.object(httpx.Client, "request", autospec=True) as request:
            request.return_value = response
            user = client.get_authenticated_user()

        self.assertEqual(user["login"], "k88936")
        request.assert_called_once()
        # (self, method, url, ...) — the URL came from the client, not urllib.
        self.assertIn(
            "http://example.invalid/api/v1/user", request.call_args.args
        )
        self.assertIn("GET", request.call_args.args)

    def test_module_repo_is_gitpython(self):
        self.assertIs(gitrepo.Repo, Repo)

    def test_clone_goes_through_gitpython(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp) / "clone"
            with mock.patch.object(gitrepo, "Repo") as repo:
                repo.return_value.remotes = []
                remote = gitrepo.ensure_checkout(
                    clone, "https://git.example.com/o/r.git", owner="o", repo="r"
                )
                repo.clone_from.assert_called_once_with(
                    "https://git.example.com/o/r.git", str(clone), env=mock.ANY
                )
            self.assertEqual(remote, gitrepo.GITEA_REMOTE)

    def test_run_git_goes_through_gitpython_execute(self):
        with mock.patch.object(gitrepo, "Git") as git:
            git.return_value.execute.return_value = "abc123\n"
            result = gitrepo.run_git(["rev-parse", "HEAD"], cwd="/tmp")
        git.return_value.execute.assert_called_once_with(["git", "rev-parse", "HEAD"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "abc123\n")


if __name__ == "__main__":
    unittest.main()

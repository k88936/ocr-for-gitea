import io
import tempfile
import unittest
from pathlib import Path

from gitea_ocr_review.config import (
    default_config_path,
    load_config,
    redact,
    resolve_token,
)
from gitea_ocr_review.errors import ConfigError


class ResolveTokenTests(unittest.TestCase):
    def test_precedence_cli_over_everything(self):
        token, source = resolve_token(
            cli_token="cli-token",
            token_file=None,
            token_env="MY_ENV",
            config={"token": "cfg-token"},
            env={"MY_ENV": "env-token"},
            stdin=io.StringIO("stdin-token\n"),
        )
        self.assertEqual(token, "cli-token")
        self.assertEqual(source, "--token")

    def test_token_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token"
            path.write_text("  file-token\n")
            token, source = resolve_token(token_file=path, env={}, stdin=io.StringIO(""))
            self.assertEqual(token, "file-token")
            self.assertIn("token-file", source)

    def test_token_file_empty_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token"
            path.write_text("   \n")
            with self.assertRaises(ConfigError):
                resolve_token(token_file=path, env={}, stdin=io.StringIO(""))

    def test_stdin(self):
        token, source = resolve_token(token_stdin=True, env={}, stdin=io.StringIO("stdin-token\n"))
        self.assertEqual(token, "stdin-token")
        self.assertEqual(source, "--token-stdin")

    def test_explicit_env(self):
        token, source = resolve_token(
            token_env="MY_ENV", config={}, env={"MY_ENV": "env-token"}, stdin=io.StringIO("")
        )
        self.assertEqual(token, "env-token")
        self.assertIn("MY_ENV", source)

    def test_default_env(self):
        token, source = resolve_token(config={}, env={"GITEA_TOKEN": "abc"}, stdin=io.StringIO(""))
        self.assertEqual(token, "abc")
        self.assertIn("GITEA_TOKEN", source)

    def test_config_fallback(self):
        token, source = resolve_token(config={"token": "cfg"}, env={}, stdin=io.StringIO(""))
        self.assertEqual(token, "cfg")
        self.assertEqual(source, "config file")

    def test_missing_token_raises(self):
        with self.assertRaises(ConfigError):
            resolve_token(config={}, env={}, stdin=io.StringIO(""))


class ConfigFileTests(unittest.TestCase):
    def test_load_missing_explicit_path(self):
        with self.assertRaises(ConfigError):
            load_config(Path("/nonexistent/gitea-ocr-review/config.toml"))

    def test_load_valid_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('gitea_url = "http://x"\nreject_on = ["high"]\n')
            config = load_config(path)
            self.assertEqual(config["gitea_url"], "http://x")
            self.assertEqual(config["reject_on"], ["high"])

    def test_default_config_path_uses_xdg(self):
        import os

        old = os.environ.get("XDG_CONFIG_HOME")
        try:
            os.environ["XDG_CONFIG_HOME"] = "/tmp/xdgtest"
            self.assertEqual(default_config_path(), Path("/tmp/xdgtest/gitea-ocr-review/config.toml"))
        finally:
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old


class RedactTests(unittest.TestCase):
    def test_redact(self):
        self.assertEqual(redact("abcdefghijkl"), "abcd...ijkl")
        self.assertEqual(redact("abc"), "***")
        self.assertEqual(redact(None), "<none>")
        self.assertNotIn("cdefgh", redact("abcdefghijkl"))


if __name__ == "__main__":
    unittest.main()

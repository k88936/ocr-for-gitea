import tempfile
import threading
import unittest
from pathlib import Path

from gitea_ocr_review.state import ReviewState


class ReviewStateTests(unittest.TestCase):
    def test_mark_save_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "watch-state.json"
            state = ReviewState(path)
            self.assertIsNone(state.head_sha("o", "r", 1))
            self.assertFalse(state.was_reviewed("o", "r", 1, "abc"))

            state.mark("o", "r", 1, head_sha="abc", event="APPROVED", review_id=5, findings=2)
            state.save()

            self.assertTrue(path.exists())
            reloaded = ReviewState(path)
            self.assertEqual(reloaded.head_sha("o", "r", 1), "abc")
            self.assertTrue(reloaded.was_reviewed("o", "r", 1, "abc"))
            self.assertFalse(reloaded.was_reviewed("o", "r", 1, "def"))
            entry = reloaded.get("o", "r", 1)
            self.assertEqual(entry["event"], "APPROVED")
            self.assertEqual(entry["review_id"], 5)
            self.assertEqual(entry["findings"], 2)

    def test_distinct_repositories_do_not_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ReviewState(Path(tmp) / "s.json", persist=False)
            state.mark("o", "r", 1, head_sha="a", event="APPROVED")
            state.mark("o", "other", 1, head_sha="b", event="APPROVED")
            self.assertEqual(state.head_sha("o", "r", 1), "a")
            self.assertEqual(state.head_sha("o", "other", 1), "b")

    def test_persist_false_keeps_memory_but_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            state = ReviewState(path, persist=False)
            state.mark("o", "r", 1, head_sha="abc", event="APPROVED", dry_run=True)
            state.save()
            self.assertFalse(path.exists())
            self.assertEqual(state.head_sha("o", "r", 1), "abc")

    def test_corrupt_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text("this is not json")
            state = ReviewState(path)
            self.assertEqual(state.all_entries(), {})
            state.mark("o", "r", 1, head_sha="abc", event="APPROVED")
            state.save()
            self.assertEqual(ReviewState(path).head_sha("o", "r", 1), "abc")

    def test_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ReviewState(Path(tmp) / "s.json", persist=False)
            state.mark("o", "r", 1, head_sha="abc", event="APPROVED")
            state.remove("o", "r", 1)
            self.assertIsNone(state.head_sha("o", "r", 1))

    def test_concurrent_writers_keep_every_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            state = ReviewState(path)
            errors: list[BaseException] = []

            def worker(i: int) -> None:
                try:
                    state.mark("o", "r", i, head_sha=f"{i:040x}", event="APPROVED")
                    state.save()
                except BaseException as exc:  # pragma: no cover - failure path
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(25)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(len(state.all_entries()), 25)
            # The file on disk must be valid JSON with all 25 entries too.
            self.assertEqual(len(ReviewState(path).all_entries()), 25)


if __name__ == "__main__":
    unittest.main()

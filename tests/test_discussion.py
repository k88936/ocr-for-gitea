import unittest

from gitea_ocr_review.discussion import build_discussion_markdown


def comment(body, *, login="alice", created_at="2026-01-01T00:00:00Z", **kwargs):
    payload = {
        "body": body,
        "user": {"login": login},
        "created_at": created_at,
    }
    payload.update(kwargs)
    return payload


class DiscussionMarkdownTests(unittest.TestCase):
    def test_includes_description_and_comments_in_order(self):
        markdown = build_discussion_markdown(
            {
                "number": 7,
                "title": "Fix add",
                "html_url": "http://gitea/o/r/pulls/7",
                "user": {"login": "carol"},
                "body": "Please keep add() working.",
            },
            issue_comments=[
                comment("first", login="alice", created_at="2026-01-01T00:00:00Z"),
                comment("second", login="bob", created_at="2026-01-02T00:00:00Z"),
            ],
            review_comments=[
                comment(
                    "this line is wrong",
                    login="dave",
                    created_at="2026-01-03T00:00:00Z",
                    path="calc.py",
                    line=2,
                )
            ],
        )
        self.assertIn("# Pull request #7: Fix add", markdown)
        self.assertIn("Please keep add() working.", markdown)
        self.assertLess(markdown.index("first"), markdown.index("second"))
        self.assertLess(markdown.index("second"), markdown.index("this line is wrong"))
        self.assertIn("`calc.py:2`", markdown)
        self.assertIn("comment by `bob`", markdown)
        self.assertIn("review comment by `dave`", markdown)

    def test_skips_empty_comment_bodies(self):
        markdown = build_discussion_markdown(
            {"number": 1, "title": "T"},
            issue_comments=[comment(""), comment("   ")],
            review_comments=[],
        )
        self.assertIn("_No comments yet._", markdown)
        self.assertNotIn("comment by", markdown)

    def test_handles_missing_fields(self):
        markdown = build_discussion_markdown({})
        self.assertIn("# Pull request #None", markdown)
        self.assertIn("_No comments yet._", markdown)

    def test_merges_comments_by_timestamp(self):
        markdown = build_discussion_markdown(
            {"number": 1, "title": "T"},
            issue_comments=[
                comment("later", login="alice", created_at="2026-01-05T00:00:00Z")
            ],
            review_comments=[
                comment(
                    "earlier",
                    login="bob",
                    created_at="2026-01-01T00:00:00Z",
                    path="a.py",
                    line=1,
                )
            ],
        )
        self.assertLess(markdown.index("earlier"), markdown.index("later"))


if __name__ == "__main__":
    unittest.main()

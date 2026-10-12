import unittest

from gitea_ocr_review.ocr_runner import Finding
from gitea_ocr_review.policy import (
    EVENT_APPROVE,
    EVENT_REQUEST_CHANGES,
    build_inline_comments,
    build_review_body,
    decide_event,
    parse_reject_on,
    summarize_findings,
)


def finding(**kwargs):
    base = {
        "path": "calc.py",
        "content": "add() subtracts instead of adding",
        "severity": "critical",
        "category": "bug",
        "start_line": 2,
        "end_line": 2,
        "suggestion_code": "    return a + b",
    }
    base.update(kwargs)
    return Finding(**base)


class DecideEventTests(unittest.TestCase):
    def test_rejects_on_critical(self):
        event, reason = decide_event([finding()])
        self.assertEqual(event, EVENT_REQUEST_CHANGES)
        self.assertIn("critical", reason)

    def test_approves_when_clean(self):
        event, _ = decide_event([])
        self.assertEqual(event, EVENT_APPROVE)

    def test_approves_non_blocking_findings(self):
        event, _ = decide_event([finding(severity="medium")])
        self.assertEqual(event, EVENT_APPROVE)

    def test_reject_threshold_is_lowest_severity(self):
        findings = [finding(severity="high"), finding(severity="critical")]
        event, _ = decide_event(findings, reject_on=("high", "critical"))
        self.assertEqual(event, EVENT_REQUEST_CHANGES)

    def test_parse_reject_on(self):
        self.assertEqual(parse_reject_on("high, critical"), ("high", "critical"))
        self.assertEqual(parse_reject_on(["High", " MEDIUM "]), ("high", "medium"))
        self.assertEqual(parse_reject_on(None), ("critical", "high"))

    def test_summarize(self):
        counts = summarize_findings([finding(), finding(severity="critical"), finding(severity="medium")])
        self.assertEqual(counts["critical"], 2)
        self.assertEqual(counts["medium"], 1)


class InlineCommentTests(unittest.TestCase):
    def test_maps_to_new_position(self):
        comments = build_inline_comments([finding()])
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["path"], "calc.py")
        self.assertEqual(comments[0]["new_position"], 2)
        self.assertEqual(comments[0]["old_position"], 0)
        self.assertIn("return a + b", comments[0]["body"])
        self.assertIn("Suggested change", comments[0]["body"])

    def test_filters_files_not_in_head(self):
        comments = build_inline_comments([finding()], head_paths=["other.py"])
        self.assertEqual(comments, [])

    def test_skips_findings_without_line(self):
        comments = build_inline_comments([finding(start_line=None, end_line=None)])
        self.assertEqual(comments, [])

    def test_caps_comments(self):
        findings = [finding(path=f"f{i}.py") for i in range(10)]
        comments = build_inline_comments(findings, max_comments=3)
        self.assertEqual(len(comments), 3)


class ReviewBodyTests(unittest.TestCase):
    def test_body_contains_decision_and_findings(self):
        pr = {"number": 1, "title": "Change add", "html_url": "http://x/1"}
        body = build_review_body(
            pr=pr,
            event=EVENT_REQUEST_CHANGES,
            findings=[finding()],
            repo="o/r",
            base="a" * 40,
            head="b" * 40,
            result_meta={"provider": "company", "model": "deepseek", "files_reviewed": 1},
            inline_count=1,
        )
        self.assertIn("Changes requested", body)
        self.assertIn("REQUEST_CHANGES", body)
        self.assertIn("calc.py:2", body)
        self.assertIn("company/deepseek", body)
        self.assertIn("return a + b", body)

    def test_body_clean_pr(self):
        body = build_review_body(
            pr={"number": 2, "title": "Clean"},
            event=EVENT_APPROVE,
            findings=[],
            repo="o/r",
            base="a" * 40,
            head="b" * 40,
        )
        self.assertIn("Approved", body)
        self.assertIn("**Findings:** 0", body)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Assert that the latest review by the token's user has an expected state."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gitea_api import Api  # noqa: E402


def fail(message: str) -> None:
    print(f"[verify] FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--expect", required=True)
    parser.add_argument("--min-comments", type=int, default=0)
    args = parser.parse_args()

    api = Api(args.url, args.token)
    me = api.get("/user")["login"]
    reviews = api.get(
        f"/repos/{args.owner}/{args.repo}/pulls/{args.pr}/reviews"
    ) or []
    mine = [r for r in reviews if (r.get("user") or {}).get("login") == me]
    if not mine:
        fail(f"no review by {me} found on PR #{args.pr}")

    latest = max(mine, key=lambda r: r.get("id", 0))
    state = latest.get("state")
    comments = latest.get("comments_count", 0)
    print(
        f"[verify] PR #{args.pr}: latest review by {me} is id={latest.get('id')} "
        f"state={state} comments={comments}"
    )

    if state != args.expect:
        fail(f"expected state {args.expect!r}, got {state!r}")
    if comments < args.min_comments:
        fail(f"expected at least {args.min_comments} inline comment(s), got {comments}")

    print(f"[verify] OK: PR #{args.pr} -> {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

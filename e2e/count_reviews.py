#!/usr/bin/env python3
"""Print the number of reviews by the token's user on a pull request."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gitea_api import Api  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--all", action="store_true", help="count all reviews, not just mine")
    args = parser.parse_args()

    api = Api(args.url, args.token)
    me = api.get("/user")["login"]
    reviews = api.get(f"/repos/{args.owner}/{args.repo}/pulls/{args.pr}/reviews") or []
    if not args.all:
        reviews = [r for r in reviews if (r.get("user") or {}).get("login") == me]
    print(len(reviews))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

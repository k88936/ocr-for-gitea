#!/usr/bin/env python3
"""Provision a Gitea repository with pull requests used by the e2e harness.

Creates three PRs:
  * pr_buggy  - modifies add() to subtract
  * pr_clean  - adds a safe clamp() helper
  * pr_watch  - adds a buggy multiply() helper (used by the watch test)

Optionally seeds a watch-state file so the watch test can skip the first two.

Prints a single-line JSON object on stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gitea_api import Api, ApiError, b64  # noqa: E402

CALC_GOOD = """def add(a, b):
    return a + b


def div(a, b):
    return a / b
"""

CALC_BAD = """def add(a, b):
    return a - b


def div(a, b):
    return a / b
"""

UTILS_CLEAN = """def clamp(value, low, high):
    if low > high:
        raise ValueError("low must not exceed high")
    return max(low, min(value, high))
"""

MULTIPLY_BAD = """def multiply(a, b):
    return a + b
"""


def log(message: str) -> None:
    print(f"[provision] {message}", file=sys.stderr)


def provision_team_repo(api: Api, *, org: str, team: str, reviewer: str) -> dict:
    """Create an org repo whose PR requests a review from ``team``."""

    try:
        api.post("/orgs", {"username": org, "visibility": "private"})
        log(f"created organization {org}")
    except ApiError as exc:
        if exc.status not in (409, 422):
            raise

    try:
        team_obj = api.post(
            f"/orgs/{org}/teams",
            {
                "name": team,
                "permission": "write",
                "units": ["repo.code", "repo.issues", "repo.pulls"],
                "includes_all_repositories": True,
            },
        )
    except ApiError as exc:
        if exc.status not in (409, 422):
            raise
        team_obj = next(t for t in api.get(f"/orgs/{org}/teams") if t["name"] == team)

    api.put(f"/teams/{team_obj['id']}/members/{reviewer}")

    repo = "teamdemo"
    base = f"/repos/{org}/{repo}"
    try:
        api.delete(base)
    except ApiError as exc:
        if exc.status != 404:
            raise

    api.post(
        f"/orgs/{org}/repos",
        {"name": repo, "auto_init": True, "default_branch": "main", "private": True},
    )
    api.post(
        f"{base}/contents/calc.py",
        {"content": b64(CALC_GOOD), "branch": "main", "message": "add calc"},
    )
    api.post(f"{base}/branches", {"new_branch_name": "feature", "old_ref_name": "main"})
    sha = api.get(f"{base}/contents/calc.py?ref=feature")["sha"]
    api.put(
        f"{base}/contents/calc.py",
        {"content": b64(CALC_BAD), "branch": "feature", "sha": sha, "message": "change add"},
    )
    pr = api.post(
        f"{base}/pulls",
        {"head": "feature", "base": "main", "title": "Change add function (team request)",
         "body": "Reviews should flag the subtraction bug."},
    )
    api.post(
        f"{base}/pulls/{pr['number']}/requested_reviewers",
        {"team_reviewers": [team]},
    )
    log(f"requested review from team {org}/{team} on {org}/{repo}#{pr['number']}")

    return {
        "team_owner": org,
        "team_repo": repo,
        "team_name": team,
        "team_pr": pr["number"],
        "team_head": pr["head"]["sha"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--seed-state", type=Path,
                        help="write a watch-state file marking --seed-prs as reviewed")
    parser.add_argument("--seed-prs", default="",
                        help="comma-separated PR numbers to pre-mark in --seed-state")
    parser.add_argument("--team-org", default="",
                        help="also create an org repo whose PR requests a team review")
    parser.add_argument("--team-name", default="reviewers",
                        help="team to request the review from (default: reviewers)")
    args = parser.parse_args()

    api = Api(args.url, args.token)
    owner, repo = args.owner, args.repo
    base = f"/repos/{owner}/{repo}"

    try:
        api.delete(base)
        log(f"deleted previous {owner}/{repo}")
    except ApiError as exc:
        if exc.status != 404:
            raise

    log("creating repository")
    api.post(
        "/user/repos",
        {"name": repo, "auto_init": True, "default_branch": "main", "private": True},
    )

    log("seeding main")
    api.post(
        f"{base}/contents/calc.py",
        {"content": b64(CALC_GOOD), "branch": "main", "message": "add calc"},
    )

    log(f"granting {args.reviewer} write access")
    api.put(f"{base}/collaborators/{args.reviewer}", {"permission": "write"})

    # PR 1: buggy change to an existing file.
    log("creating feature branch and introducing a bug")
    api.post(f"{base}/branches", {"new_branch_name": "feature", "old_ref_name": "main"})
    sha = api.get(f"{base}/contents/calc.py?ref=feature")["sha"]
    api.put(
        f"{base}/contents/calc.py",
        {"content": b64(CALC_BAD), "branch": "feature", "sha": sha, "message": "change add"},
    )
    pr_buggy = api.post(
        f"{base}/pulls",
        {"head": "feature", "base": "main", "title": "Change add function",
         "body": "Reviews should flag the subtraction bug."},
    )

    # PR 2: clean new file.
    log("creating clean feature branch and pull request")
    api.post(f"{base}/branches", {"new_branch_name": "feature2", "old_ref_name": "main"})
    api.post(
        f"{base}/contents/utils.py",
        {"content": b64(UTILS_CLEAN), "branch": "feature2", "message": "add clamp util"},
    )
    pr_clean = api.post(
        f"{base}/pulls",
        {"head": "feature2", "base": "main", "title": "Add clamp helper",
         "body": "A small, safe utility."},
    )

    # PR 3: buggy new file, used to test the polling watch command.
    log("creating watch test branch and pull request")
    api.post(f"{base}/branches", {"new_branch_name": "feature3", "old_ref_name": "main"})
    api.post(
        f"{base}/contents/multiply.py",
        {"content": b64(MULTIPLY_BAD), "branch": "feature3", "message": "add multiply util"},
    )
    pr_watch = api.post(
        f"{base}/pulls",
        {"head": "feature3", "base": "main", "title": "Add multiply helper",
         "body": "Reviews should flag the wrong operator."},
    )

    # Every PR asks the reviewer for a review; watch only acts on open requests.
    for pr in (pr_buggy, pr_clean, pr_watch):
        api.post(
            f"{base}/pulls/{pr['number']}/requested_reviewers",
            {"reviewers": [args.reviewer]},
        )
    log(f"requested review from {args.reviewer} on all pull requests")

    heads = {
        str(pr_buggy["number"]): pr_buggy["head"]["sha"],
        str(pr_clean["number"]): pr_clean["head"]["sha"],
        str(pr_watch["number"]): pr_watch["head"]["sha"],
    }

    if args.seed_state:
        seed = [n.strip() for n in args.seed_prs.split(",") if n.strip()]
        state = {"version": 1, "pulls": {}}
        for number in seed:
            state["pulls"][f"{owner}/{repo}#{number}"] = {
                "head_sha": heads[number],
                "event": "SEEDED",
                "review_id": None,
                "findings": 0,
                "dry_run": False,
                "reviewed_at": "2026-01-01T00:00:00+00:00",
            }
        args.seed_state.write_text(json.dumps(state, indent=2))
        log(f"seeded watch state {args.seed_state} with PRs {seed}")

    payload = {
        "owner": owner,
        "repo": repo,
        "pr_buggy": pr_buggy["number"],
        "pr_clean": pr_clean["number"],
        "pr_watch": pr_watch["number"],
        "heads": heads,
    }
    if args.team_org:
        payload.update(
            provision_team_repo(
                api, org=args.team_org, team=args.team_name, reviewer=args.reviewer
            )
        )
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

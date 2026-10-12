#!/usr/bin/env bash
#
# End-to-end test for gitea-ocr-review using podman.
#
# It starts a disposable Gitea container, provisions three pull requests
# (buggy, clean and one reserved for the poller), runs the one-shot `review`
# command and the polling `watch` command, then verifies through the Gitea API
# that REQUEST_CHANGES / APPROVED reviews were recorded and nothing was
# reviewed twice.
#
# Requirements: podman, git, an installed `ocr` CLI with a configured LLM,
# and network access for the LLM.
#
# Usage:
#   e2e/run_e2e.sh          # clean up the container when done
#   KEEP=1 e2e/run_e2e.sh   # keep the container for inspection
#
set -euo pipefail

CONTAINER="${CONTAINER:-gitea-e2e}"
IMAGE="${IMAGE:-docker.io/gitea/gitea:1.22.6}"
PORT="${PORT:-3000}"
URL="http://127.0.0.1:${PORT}"
ADMIN="${ADMIN:-gitadmin}"
REVIEWER="${REVIEWER:-reviewbot}"
PASSWORD="${PASSWORD:-Password123!}"
OWNER="${OWNER:-$ADMIN}"
REPO="${REPO:-demo}"
TEAM_ORG="${TEAM_ORG:-acme}"
TEAM_NAME="${TEAM_NAME:-reviewers}"
KEEP="${KEEP:-0}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    PYTHON="$ROOT_DIR/.venv/bin/python"
  else
    PYTHON="$(command -v python3)"
  fi
fi
APP="$ROOT_DIR/gitea-ocr"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/gitea-ocr-review"
WATCH_STATE="${WATCH_STATE:-/tmp/gitea-ocr-watch-state.$$.json}"
WATCH_LOG="${WATCH_LOG:-/tmp/gitea-ocr-watch.$$.log}"
WATCH_JSON="${WATCH_JSON:-/tmp/gitea-ocr-watch.$$.jsonl}"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32mPASS: %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

cleanup() {
  rm -f "$WATCH_STATE" "$WATCH_LOG" "$WATCH_JSON" \
        "${WATCH_STATE%.json}-team.json" "${WATCH_LOG%.log}-team.log" \
        >/dev/null 2>&1 || true
  if [ "$KEEP" = "0" ]; then
    podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
    podman volume rm -f "${CONTAINER}-data" >/dev/null 2>&1 || true
  else
    echo "keeping container '$CONTAINER' (URL $URL, admin token below)"
  fi
}
trap cleanup EXIT

command -v podman >/dev/null || fail "podman is required"
command -v curl   >/dev/null || fail "curl is required"
command -v ocr    >/dev/null || fail "the ocr CLI is required (npm i -g @alibaba-group/open-code-review)"
[ -x "$APP" ] || fail "$APP is not executable"

step "clearing cached clones"
rm -rf "$CACHE_DIR"

step "starting Gitea ($IMAGE) on port $PORT"
podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
podman volume rm -f "${CONTAINER}-data" >/dev/null 2>&1 || true
podman run -d --name "$CONTAINER" -p "${PORT}:3000" \
  -e GITEA__server__ROOT_URL="${URL}/" \
  -e GITEA__server__DISABLE_SSH=true \
  -e GITEA__server__START_SSH_SERVER=false \
  -e GITEA__database__DB_TYPE=sqlite3 \
  -e GITEA__security__INSTALL_LOCK=true \
  -v "${CONTAINER}-data:/data" \
  "$IMAGE" >/dev/null

step "waiting for Gitea to become ready"
ready=0
for _ in $(seq 1 90); do
  if curl -sf -o /dev/null "${URL}/api/healthz" || curl -sf -o /dev/null "${URL}/api/v1/version"; then
    ready=1
    break
  fi
  sleep 1
done
[ "$ready" = "1" ] || { podman logs "$CONTAINER" | tail -40; fail "Gitea did not become ready"; }
ok "Gitea is up at $URL"

step "creating admin and reviewer users"
podman exec -u git "$CONTAINER" gitea admin user create \
  --admin --username "$ADMIN" --password "$PASSWORD" \
  --email "${ADMIN}@example.com" --must-change-password=false >/dev/null
podman exec -u git "$CONTAINER" gitea admin user create \
  --username "$REVIEWER" --password "$PASSWORD" \
  --email "${REVIEWER}@example.com" --must-change-password=false >/dev/null

ADMIN_TOKEN="$(podman exec -u git "$CONTAINER" gitea admin user generate-access-token \
  --username "$ADMIN" --token-name e2e-admin --scopes all --raw | tail -1)"
REVIEWER_TOKEN="$(podman exec -u git "$CONTAINER" gitea admin user generate-access-token \
  --username "$REVIEWER" --token-name e2e-reviewer --scopes all --raw | tail -1)"
[ "${#REVIEWER_TOKEN}" -ge 20 ] || fail "could not generate reviewer token"
ok "tokens generated (reviewer token len ${#REVIEWER_TOKEN})"

step "provisioning repository and pull requests"
rm -f "$WATCH_STATE"
PROV_JSON="$("$PYTHON" "$ROOT_DIR/e2e/provision.py" \
  --url "$URL" --token "$ADMIN_TOKEN" --owner "$OWNER" --repo "$REPO" --reviewer "$REVIEWER" \
  --seed-state "$WATCH_STATE" --seed-prs "1,2" \
  --team-org "$TEAM_ORG" --team-name "$TEAM_NAME")"
echo "$PROV_JSON"
PR_BUGGY="$("$PYTHON" -c 'import sys,json;print(json.load(sys.stdin)["pr_buggy"])' <<<"$PROV_JSON")"
PR_CLEAN="$("$PYTHON" -c 'import sys,json;print(json.load(sys.stdin)["pr_clean"])' <<<"$PROV_JSON")"
PR_WATCH="$("$PYTHON" -c 'import sys,json;print(json.load(sys.stdin)["pr_watch"])' <<<"$PROV_JSON")"
TEAM_REPO="$("$PYTHON" -c 'import sys,json;print(json.load(sys.stdin)["team_repo"])' <<<"$PROV_JSON")"
TEAM_PR="$("$PYTHON" -c 'import sys,json;print(json.load(sys.stdin)["team_pr"])' <<<"$PROV_JSON")"
ok "provisioned PR #$PR_BUGGY (buggy), PR #$PR_CLEAN (clean), PR #$PR_WATCH (watch), $TEAM_ORG/$TEAM_REPO#$TEAM_PR (team request)"

step "auth check (doctor) with access token"
GITEA_TOKEN="$REVIEWER_TOKEN" "$APP" doctor --gitea-url "$URL" --no-proxy \
  2>&1 | grep -v -E 'UNDICI|trace-warnings' || true

step "running app on buggy PR #$PR_BUGGY (expect REQUEST_CHANGES)"
GITEA_TOKEN="$REVIEWER_TOKEN" "$APP" review "$OWNER/$REPO#$PR_BUGGY" \
  --gitea-url "$URL" --no-proxy -v --json \
  2>&1 | grep -v -E 'UNDICI|trace-warnings'
"$PYTHON" "$ROOT_DIR/e2e/verify.py" --url "$URL" --token "$REVIEWER_TOKEN" \
  --owner "$OWNER" --repo "$REPO" --pr "$PR_BUGGY" \
  --expect REQUEST_CHANGES --min-comments 1
ok "buggy PR was rejected with inline feedback"

step "running app on clean PR #$PR_CLEAN (expect APPROVED)"
GITEA_TOKEN="$REVIEWER_TOKEN" "$APP" review "$OWNER/$REPO#$PR_CLEAN" \
  --gitea-url "$URL" --no-proxy -v --json \
  2>&1 | grep -v -E 'UNDICI|trace-warnings'
"$PYTHON" "$ROOT_DIR/e2e/verify.py" --url "$URL" --token "$REVIEWER_TOKEN" \
  --owner "$OWNER" --repo "$REPO" --pr "$PR_CLEAN" --expect APPROVED
ok "clean PR was approved"

step "checking self-approval is refused with a helpful error"
set +e
OUT="$(GITEA_TOKEN="$ADMIN_TOKEN" "$APP" review "$OWNER/$REPO#$PR_CLEAN" \
  --gitea-url "$URL" --no-proxy 2>&1)"
CODE=$?
set -e
echo "$OUT" | grep -v -E 'UNDICI|trace-warnings' || true
[ "$CODE" -eq 4 ] || fail "expected exit code 4 for self-approval, got $CODE"
echo "$OUT" | grep -qi "approve your own pull" || fail "missing self-approval explanation"
ok "self-approval correctly refused"

step "watch mode: one cycle auto-reviews new PR #$PR_WATCH and skips seeded PRs"
GITEA_TOKEN="$REVIEWER_TOKEN" "$APP" watch "$OWNER/$REPO" --once --concurrency 2 \
  --gitea-url "$URL" --no-proxy --state-file "$WATCH_STATE" \
  --cache-dir "$CACHE_DIR" 2>&1 | tee "$WATCH_LOG" | grep -v -E 'UNDICI|trace-warnings' || true
"$PYTHON" "$ROOT_DIR/e2e/verify.py" --url "$URL" --token "$REVIEWER_TOKEN" \
  --owner "$OWNER" --repo "$REPO" --pr "$PR_WATCH" \
  --expect REQUEST_CHANGES --min-comments 1
grep -q "already reviewed" "$WATCH_LOG" || fail "watch did not report skipping seeded PRs"
grep -q "concurrency=2" "$WATCH_LOG" || fail "watch did not report --concurrency 2"
ok "watch detected the new review request and rejected it (thread pool)"

step "watch mode: a team review request is expanded to its members"
TEAM_STATE="${WATCH_STATE%.json}-team.json"
TEAM_LOG="${WATCH_LOG%.log}-team.log"
rm -f "$TEAM_STATE"
GITEA_TOKEN="$REVIEWER_TOKEN" "$APP" watch "$TEAM_ORG/$TEAM_REPO" --once \
  --gitea-url "$URL" --no-proxy --state-file "$TEAM_STATE" \
  --cache-dir "$CACHE_DIR" 2>&1 | tee "$TEAM_LOG" | grep -v -E 'UNDICI|trace-warnings' || true
"$PYTHON" "$ROOT_DIR/e2e/verify.py" --url "$URL" --token "$REVIEWER_TOKEN" \
  --owner "$TEAM_ORG" --repo "$TEAM_REPO" --pr "$TEAM_PR" \
  --expect REQUEST_CHANGES --min-comments 1
grep -q "team" "$TEAM_LOG" || fail "watch did not report the team request"
ok "team review request reached the team member and was reviewed"

step "watch mode: second cycle must not post a duplicate review"
BEFORE="$("$PYTHON" "$ROOT_DIR/e2e/count_reviews.py" --url "$URL" --token "$REVIEWER_TOKEN" \
  --owner "$OWNER" --repo "$REPO" --pr "$PR_WATCH")"
GITEA_TOKEN="$REVIEWER_TOKEN" "$APP" watch "$OWNER/$REPO" --once \
  --gitea-url "$URL" --no-proxy --state-file "$WATCH_STATE" \
  --cache-dir "$CACHE_DIR" 2>&1 | grep -v -E 'UNDICI|trace-warnings' || true
AFTER="$("$PYTHON" "$ROOT_DIR/e2e/count_reviews.py" --url "$URL" --token "$REVIEWER_TOKEN" \
  --owner "$OWNER" --repo "$REPO" --pr "$PR_WATCH")"
[ "$BEFORE" = "$AFTER" ] || fail "watch posted a duplicate review ($BEFORE -> $AFTER)"
ok "watch skipped the already-reviewed revision (reviews: $AFTER)"

step "watch mode: --log-json emits machine-readable stdout logs"
GITEA_TOKEN="$REVIEWER_TOKEN" "$APP" watch "$OWNER/$REPO" --once \
  --gitea-url "$URL" --no-proxy --state-file "$WATCH_STATE" \
  --cache-dir "$CACHE_DIR" --log-json > "$WATCH_JSON" 2>/dev/null
"$PYTHON" - "$WATCH_JSON" <<'PY'
import json, sys
lines = [line for line in open(sys.argv[1]) if line.strip()]
assert lines, "no JSON log lines were produced"
for line in lines:
    record = json.loads(line)
    assert "ts" in record and "msg" in record, record
print(f"validated {len(lines)} JSON log line(s)")
PY
ok "watch --log-json output is valid JSON lines"

echo
printf '\033[1;32m========================================\033[0m\n'
printf '\033[1;32m  ALL END-TO-END CHECKS PASSED (podman) \033[0m\n'
printf '\033[1;32m========================================\033[0m\n'

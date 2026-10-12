# gitea-ocr-review

A small local command line app that turns
[alibaba/open-code-review](https://github.com/alibaba/open-code-review) (`ocr`)
into an automated **Gitea pull-request gate**:

1. **Query** a Gitea review request (pull request) through the Gitea REST API.
2. Run `ocr review --from <base> --to <head> --format json` on a local clone.
3. Turn the JSON findings into a verdict using a configurable policy.
4. **Post** the verdict back to Gitea as an `APPROVED` or `REQUEST_CHANGES`
   review carrying only code-side inline comments (no summary comment).

Authentication uses a **Gitea access token**. HTTP is handled by
[`httpx`](https://www.python-httpx.org/) and local git operations by
[`GitPython`](https://gitpython.readthedocs.io/); both are declared in
`pyproject.toml` and installed with [uv](https://docs.astral.sh/uv/).

---

## Requirements

| Tool | Why |
| --- | --- |
| Python ≥ 3.11 | `tomllib` |
| [uv](https://docs.astral.sh/uv/) | dependency / virtualenv management |
| [`ocr`](https://github.com/alibaba/open-code-review) | the reviewer, already configured with an LLM |
| `git` | to clone / fetch the pull-request commits |
| `podman` (optional) | only for the end-to-end test |

Confirm the OCR setup first:

```console
$ ocr version
open-code-review v1.12.13 ...
$ ocr config set provider ...
```

---

## Quick start

```console
# review PR #7 of owner/repo and post the verdict
./gitea-ocr review https://gitea.example.com owner/repo#7 --token-env GITEA_TOKEN

# or keep connection settings in the config file (see below)
GITEA_TOKEN=xxxxx ./gitea-ocr review owner/repo#7

# inspect without posting anything
./gitea-ocr review owner/repo#7 --dry-run

# list the open pull requests (the "review requests")
./gitea-ocr list owner/repo

# poll continually and auto-review new/changed PRs (logs to stdout)
./gitea-ocr watch owner/repo --interval 60

# review up to 4 pull requests at once (thread pool)
./gitea-ocr watch owner/repo --interval 60 --concurrency 4
```

Dependencies are managed with uv; install them once and run through the
launcher (which uses the uv virtualenv) or `uv run`:

```console
$ uv sync --extra dev
$ ./gitea-ocr review owner/repo#7
$ uv run gitea-ocr review owner/repo#7      # equivalent
```

### Standalone binary

PyInstaller can freeze the app into a single executable that needs **no Python
installation**. It still drives the `git` and `ocr` CLIs, so those must be on
`PATH` at runtime.

```console
$ ./scripts/build_binary.sh          # -> dist/gitea-ocr (~16 MB, one file)
$ ./dist/gitea-ocr --version
$ ./dist/gitea-ocr review owner/repo#7 --gitea-url https://gitea.example.com
```

The build uses the `build` extra (`pyinstaller>=6.10`, pinned in `uv.lock`).
The end-to-end suite accepts a binary via `APP`, e.g.
`APP=$PWD/dist/gitea-ocr ./e2e/run_e2e.sh`.

---

## Access-token authentication

Pass the token **only** in one of these ways. When several are present the
first match wins:

| Order | Method | Example |
| --- | --- | --- |
| 1 | `--token` | `--token "$GITEA_TOKEN"` (visible in `ps`; least safe) |
| 2 | `--token-file` | `--token-file ~/.config/gitea-ocr-review/token` |
| 3 | `--token-stdin` | `printf %s "$TOK" \| gitea-ocr review ... --token-stdin` |
| 4 | environment variable | `GITEA_TOKEN=... gitea-ocr ...` or `--token-env MY_VAR` |
| 5 | config file key | `token = "..."` in `config.toml` |

The token is sent to Gitea as the `Authorization: token <token>` header, and is
also injected into `git` via git's `GIT_CONFIG_*` environment protocol so it
never lands in process arguments or in `.git/config`. Secrets are redacted
(`abcd...wxyz`) in all log output.

### Creating a token in Gitea

* **Web UI:** *Settings → Applications → Generate New Token*, select at least
  the `write:repository` scope.
* **CLI (inside the Gitea container/host):**

  ```console
  gitea admin user generate-access-token --username reviewbot \
        --token-name ocr --scopes all --raw
  ```

* **API:** `POST /api/v1/users/{username}/tokens` with basic auth.

Use a token belonging to a **reviewer account that is not the PR author**:
Gitea refuses self-approval (`approve your own pull is not allowed`). The app
detects that and prints a helpful error.

---

## Configuration file

Default location: `~/.config/gitea-ocr-review/config.toml`
(override with `--config`; see `config.example.toml`).

```toml
gitea_url = "https://gitea.example.com"
token_env = "GITEA_TOKEN"
# token   = "..."              # optional, lower priority than the env var

ocr_bin   = "ocr"
reject_on = ["critical", "high"]

# ocr_timeout = 900            # seconds; omit for ocr's own default
# ocr_args    = ["--effort", "high"]
```

---

## Usage

```
gitea-ocr [global options] <command> [command options]

Commands:
  review (run)   review one pull request and post the verdict
  watch (poll)   poll Gitea and auto-review new/changed pull requests
  list           list pull requests in a repository
  doctor         check config, token, Gitea connectivity and ocr
```

Global options (accepted before or after the subcommand): `--gitea-url`,
`--token`, `--token-file`, `--token-stdin`, `--token-env`, `--config`,
`--insecure`, `--no-proxy`, `--timeout`, `-v/--verbose`.

### `review`

```
gitea-ocr review owner/repo#123
gitea-ocr review owner/repo 123
gitea-ocr review --repo owner/repo --pr 123
```

| Option | Meaning |
| --- | --- |
| `--reject-on high,critical` | severities that trigger `REQUEST_CHANGES` (lowest is the threshold) |
| `--repo-dir DIR` | use an existing local clone instead of the cache |
| `--cache-dir DIR` | where clones are cached; if `DIR` is itself a git checkout of the repo it is used directly (default `~/.cache/gitea-ocr-review`) |
| `--clone-url URL` | override the clone URL reported by Gitea |
| `--ocr-bin PATH`, `--ocr-arg ...` | point at `ocr` / pass extra flags through |
| `--ocr-timeout SECONDS` | kill `ocr` after this long |
| `--no-inline-comments` | do not attach line comments; post the summary body instead |
| `--max-inline-comments N` | cap inline comments (default 25) |
| `--dry-run` | print the review without posting |
| `--json` | emit a machine-readable summary |
| `--force` | review even if the PR is not open |

### Decision policy

The verdict is always derived from the findings — there is no manual override:

* any finding whose severity is **at or above the lowest `--reject-on`
  severity** → `REQUEST_CHANGES`
* otherwise (zero findings, or only lower-severity findings) → `APPROVED`

The only events sent to Gitea are `APPROVED` and `REQUEST_CHANGES`.

### Examples

```console
# request changes on any finding at all
gitea-ocr review owner/repo#12 --reject-on info,low,medium,high,critical

# only block on critical issues, preview as JSON
gitea-ocr review owner/repo#12 --reject-on critical --dry-run --json

# review from a pre-existing clone
gitea-ocr review owner/repo#12 --repo-dir ~/src/repo --clone-url https://gitea.example.com/owner/repo.git
```

### `watch` — poll and auto-review

`watch` keeps querying Gitea for open pull requests and only acts on the ones
that **requested a review from the token's user** — directly, or through a team
the user belongs to — and whose request is still open (a `REQUEST_REVIEW` row).
Gitea leaves stale entries in `requested_reviewers` after a review is
submitted, so the open request row is the source of truth. It remembers the
last reviewed head SHA per PR in a state file, so it never reviews the same
revision twice but **does** re-review a PR automatically when new commits are
pushed. Every action is logged to **stdout** (timestamped, or JSON lines with
`--log-json`).

Team requests are expanded with a single `GET /user/teams` at startup: a PR
that requests a team containing the token's user is treated exactly like a
direct request (its log line is marked `, team`). If that call fails, only
direct requests are honoured.

Each cycle issues **one** `GET /repos/{owner}/{repo}/pulls` per repository: that
list already carries the base/head SHAs, the merge base and the clone URL, so
`watch` never fetches a pull request individually. Gitea's list payload cannot
be trusted for *team* requests — 1.22 hides the team behind a `null` element in
`requested_reviewers`, and 1.26 returns `null` for both `requested_reviewers`
and `requested_reviewers_teams` — so every pull request that you did not author
and have not already reviewed at its current head gets one lightweight
`GET /pulls/{index}/reviews`. The open `REQUEST_REVIEW` row (for you, or for a
team you are in) is the source of truth. With `--concurrency N > 1` the selected
reviews run on a `ThreadPoolExecutor`; the state file and the per-repository git
fetches are locked, so parallel workers cannot duplicate a review or corrupt
the state.

```
gitea-ocr watch owner/repo --interval 60
gitea-ocr watch owner/repo other/repo --concurrency 2
gitea-ocr watch --org my-org --interval 300
gitea-ocr watch owner/repo --once          # single pass, cron/systemd friendly
```

| Option | Meaning |
| --- | --- |
| `--interval SECONDS` | delay between poll cycles (default 60; `0` = as fast as possible) |
| `--once` | run one cycle then exit (use from cron / a systemd timer) |
| `--org ORG` / `--user USER` | watch every repository of an org / user |
| `--re-review` | ignore saved state and review even unchanged revisions |
| `--include-own` | also process PRs authored by the token user (normally skipped) |
| `--max-per-cycle N` | cap reviews per cycle (`0` = unlimited) |
| `--concurrency N` | review up to `N` PRs in parallel on a thread pool (default 1) |
| `--state-file PATH` | state JSON (default `<cache>/watch-state.json`) |
| `--log-json` | emit stdout logs as JSON lines for log shippers |
| `--pr-state open\|closed\|all` | which PRs to consider (default `open`) |

It also accepts the review knobs (`--reject-on`,
`--no-inline-comments`, `--max-inline-comments`, `--dry-run`, `--ocr-*`,
`--cache-dir`). `--dry-run` reviews without posting and keeps state in memory
only, so it never pollutes the on-disk state.

Run it as a service, for example with systemd:

```ini
[Unit]
Description=Gitea OCR review bot

[Service]
Environment=GITEA_TOKEN=xxxxxxxx
ExecStart=/opt/gitea-ocr-review/gitea-ocr watch my-org/repo --interval 120
Restart=always

[Install]
WantedBy=multi-user.target
```

or as a cron job / timer with `--once`:

```cron
*/5 * * * * GITEA_TOKEN=xxxx /opt/gitea-ocr-review/gitea-ocr watch my-org/repo --once
```

---

## How it works

```
Gitea API ──GET /repos/{o}/{r}/pulls/{n}──► base.sha, head.sha, merge_base, clone_url
     │
     ├─ clone/fetch into a cache dir (token injected via GIT_CONFIG_*)
     ├─ diff range = merge_base..head.sha   (matches the PR's three-dot diff)
     ├─ run: ocr review --repo DIR --from MERGE_BASE --to HEAD --format json
     ├─ parse comments[]  →  policy  →  APPROVED / REQUEST_CHANGES
     └─ POST /repos/{o}/{r}/pulls/{n}/reviews  {event, comments[]}

watch: GET /repos/{o}/{r}/pulls?state=open ──► for each new head SHA, run the
       pipeline above; remember reviewed SHAs in watch-state.json
```

Notes:

* The diff base is the PR's `merge_base` (falling back to
  `git merge-base base head`), so commits that landed on the base branch after
  the PR opened are not reviewed.
* Fork pull requests work: if the head commit is missing, the app fetches
  `refs/pull/<n>/head`, which Gitea exposes for every PR.
* Inline comments carry OpenCodeReview's text and its suggested change; we add
  no severity labels or prose of our own. They use Gitea's `new_position`
  (1-based line in the new file); findings on files absent from the head commit
  cannot be anchored and are dropped.
* No summary comment is posted on the pull request. Gitea refuses a
  `REQUEST_CHANGES` review without a body, so that one carries the short reason
  (e.g. `1 finding(s) at or above severity critical/high`); approvals are
  silent. `--no-inline-comments` posts the rendered summary instead.
* `watch` reuses this exact pipeline once per new head commit. It persists the
  reviewed head SHA per PR, so it is safe to restart and never double-reviews a
  revision; `--re-review` forces a fresh pass and new commits are picked up
  automatically.
* To reuse a checkout you already have, point `--cache-dir` straight at it
  (e.g. `--cache-dir ~/src/repo`) instead of at a parent directory. When that
  directory is a git clone of the same repository it is used as-is — no nested
  clone is created — and its existing remote (e.g. `origin`) is reused for
  fetches. A `gitea` remote is only added when no remote matches. Otherwise
  clones live at `<cache-dir>/<owner>__<repo>`. `--repo-dir` does the same for
  `review`.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | unexpected error |
| 2 | CLI usage error |
| 3 | configuration / token error |
| 4 | Gitea API error |
| 5 | git error |
| 6 | `ocr` error |

---

## Tests

### Unit / integration tests (no network)

```console
$ uv run pytest -q
# or, without uv:
$ python -m unittest discover -s tests -t .
```

85 tests cover token precedence, the httpx-based Gitea client, the
GitPython-based git helpers, OCR JSON parsing, the decision policy, the polling
state store and full fake-Gitea + fake-OCR pipelines for both `review` and
`watch`. They also prove that the watch path issues **no** per-PR GET, that
`--concurrency` really overlaps reviews (a `threading.Barrier` deadlocks a
serial implementation), and that concurrent `ReviewState` writers keep every
entry.

### End-to-end test with podman

`e2e/run_e2e.sh` starts a disposable Gitea container, creates two users and a
private repository with a **buggy**, a **clean** and a **watch-only** pull
request, then runs the real app (with the real `ocr` + LLM) and verifies the
recorded review states through the Gitea API:

```console
$ e2e/run_e2e.sh          # cleans the container up afterwards
$ KEEP=1 e2e/run_e2e.sh   # keep it for inspection
```

The script checks:

1. `REQUEST_CHANGES` is recorded on the buggy PR, with an inline comment.
2. `APPROVED` is recorded on the clean PR.
3. self-approval is refused with a clear message and exit code 4.
4. `watch --once` auto-reviews the new PR and skips the already-seeded ones.
5. a second `watch --once` cycle posts no duplicate review.
6. `watch --log-json` emits valid JSON lines on stdout.

Requirements: `podman`, `curl`, `git` and a working `ocr` LLM configuration.

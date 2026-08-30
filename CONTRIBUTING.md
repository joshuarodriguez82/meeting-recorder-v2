# Contributing

Read `AGENTS.md` first. It is short and two of its rules are absolute:
nothing that identifies a real person, customer, or meeting may enter
this repo (including tests, fixtures, and commit messages), and release /
CI problems get diagnosed from actual logs and API state, never from
guesses.

## Layout

| Path | What it is |
| --- | --- |
| `backend/` | Python FastAPI backend — audio capture, transcription, diarization, LLM extraction. `server.py` is the API surface; `core/` is the ML pipeline; `services/` is everything else. |
| `src/` | Next.js frontend rendered inside the Tauri webview. |
| `src-tauri/` | Rust shell: window, tray, backend process management, auth token. |
| `chrome-extension/` | MV3 extension that captures Outlook calendar data from the user's real browser session. No build step, no dependencies. |
| `mobile/` | Capacitor companion app. |
| `docs/release-notes/` | One file per release; `release.yml` publishes it verbatim as the GitHub Release body. |
| `scripts/ci/` | The CI gate scripts and their baselines. |

## Running it

Backend, standalone (no Tauri shell):

```sh
cd backend
pip install -r requirements.txt          # requirements-mac.txt on macOS
MEETING_RECORDER_AUTH_DISABLED=1 python server.py
```

Frontend + shell: `npm install && npm run tauri dev`.

`MEETING_RECORDER_SAFE_MODE=1` starts the backend without audio hardware
(what CI and the e2e rig use).

## Tests

```sh
python -m pytest backend/tests -q          # backend (fast, no ML deps needed)
cd mcp-server && python -m pytest tests -q # MCP server (run from INSIDE it)
npm test                                   # frontend (vitest)
node --test chrome-extension/tests/background.test.js
npm run build                              # typechecks via strict tsconfig
cargo test --lib --manifest-path src-tauri/Cargo.toml
```

Two traps in that list, both of which have cost a red PR:

- **The backend suite needs Python 3.12+.** `config/settings.py` uses an
  f-string form 3.11 rejects, so on an older interpreter every test that
  imports the app fails to *collect* — and a run of only the files that
  don't import it looks green.
- **The MCP suite must run from inside `mcp-server/`.** Its
  `asyncio_mode = "auto"` lives in that directory's `pyproject.toml`,
  and pytest only reads the ini options of the rootdir it resolves.
  Invoked from the repo root the config is silently ignored and every
  async test errors.

CI runs all of the above plus the security scans on every PR — 13 checks.
A PR is mergeable when all 13 are green.

House rules that will save you a review round-trip:

- **Fail-first tests.** A test for a bugfix must be shown to fail against
  the pre-fix code. Test names state the property they pin, and module
  docstrings say why the test exists ("the field report that caused
  this"), not what the code does.
- **Test fixtures are copied from real payloads, never invented.** The
  MCP server's stub carried three keys the backend has never emitted, so
  124 tests passed while every commitment rendered blank for the first
  real user. If you are writing a fixture, paste a real response into
  it; if you are fixing a bug that tests didn't catch, fix the fixture
  *first* and watch the tests go red.
- **A result you couldn't read must never render as a result that isn't
  there.** This defect class recurs in this codebase — an unreadable
  capture, a failed parse, a missing file must surface as an explicit
  error state, never as an empty success. Reviews look for it.
- **Swallowed exceptions need a reason.** `except: pass` sites must say
  why ignoring is correct, and prefer logging at debug over silence.
  bandit's baseline is a debt ledger, not a license.
- **New personal-data terms** go into the deny-list as salted hashes —
  see `docs/ci-security-and-ai-review.md` for the how.

## Releases

Releases cut from `main` only:

1. Land your PR (squash merge).
2. Bump `package.json` + `src-tauri/tauri.conf.json` versions together,
   add `docs/release-notes/release-notes-vX.Y.Z.md` (the macOS install
   block is mandatory — see `AGENTS.md`), merge that.
3. Dispatch `release.yml` with `publish=true`. It derives the tag from
   `tauri.conf.json`, verifies the tagged tree carries that version
   (the v2.7.5–2.7.7 stale-tag incident is why), builds all platforms,
   and publishes.
4. Verify: tag SHA equals the merge commit, four assets present.

## Branches

`main` is the only long-lived branch. Working branches are
`claude/<topic>` or `<yourname>/<topic>`, cut from current `origin/main`,
deleted after merge. Don't stack new work on a branch whose PR already
merged — squash merges make that history unmergeable; cut fresh.

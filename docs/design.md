# ai-reviewer — Design

A personal tool for reviewing GitHub pull requests that shows only the
changes that matter (architecture, interfaces, behavior), remembers what
you already reviewed across force-pushes, and lets you comment and post
a review back to GitHub from a local report.

## Goals

- **Signal over noise.** Hide renames, comments, docstrings, formatting
  and similar churn. Surface architecture, interface and behavior
  changes first.
- **Review once.** After a force-push, rebase or squash, show only what
  actually changed since you last looked.
- **See the shape.** Show an architecture diagram of the change and a
  table of interface changes.
- **Act from the report.** Mark units reviewed, draft comments, submit
  a GitHub review.

## Non-goals (for now)

- Languages other than Python.
- Shared or team state. Everyone runs their own copy; state is local.
- Hosted service, CI bot, or IDE extension.
- Automated bug finding. This tool helps a human review; it does not
  replace one.

## Overview

```
/review-pr 123   (Claude Code skill)
   │
   ├─► reviewer analyze 123      ── Python CLI, no LLM
   │      fetch PR via gh, split into symbol units, noise filter,
   │      import graph, interface extraction, fingerprints
   │      → analysis.json (only units that still need ranking)
   │
   ├─► Claude ranks the unranked units → ranking.json
   │      tier + reason + one-line behavior summary, PR overview
   │
   ├─► reviewer ingest 123 ranking.json   → SQLite
   │
   └─► reviewer serve 123        ── local server + static report
          mark reviewed, draft comments, submit review → GitHub (via gh)
```

Code does everything that is deterministic and testable. The LLM does
only the judgement work: ranking and summarizing what is left after
filtering.

## Core concepts

### Units

The unit of review is a **Python symbol**, not a diff hunk: a function,
method, class body (excluding its methods), or module-level block,
extracted with `ast`. Non-Python files are file-level units.

Each changed unit has:

| Field | Meaning |
|---|---|
| `path` | file path at head (or base, if deleted) |
| `qualname` | e.g. `http.client.Client.get` |
| `kind` | `function`, `method`, `class`, `module`, `file` |
| `status` | `added`, `removed`, `modified`, `renamed`, `moved` |
| `old_body`, `new_body` | source text at base and head |
| `fingerprint` | see below |
| `noise` | `null` or a reason (`docstring`, `formatting`, …) |
| `signals` | structural flags from the interface/architecture analysis |

### Normalization and fingerprints

Normalizing a body:

1. Parse with `ast`.
2. Remove docstrings. (Comments are already gone after parsing.)
3. Rename local variables and parameters of the unit to canonical names
   (`_v0`, `_v1`, …) in order of first appearance. Public names,
   attributes and globals keep their names.
4. `ast.dump` the result, so formatting differences (including
   redundant parentheses) disappear.

`fingerprint = sha256(normalize(old_body) + "\0" + normalize(new_body))`

The fingerprint identifies "this particular change to this symbol". It
does not depend on commit SHAs, so it survives force-pushes, rebases
and squashes.

Note: renaming parameters of a public function changes its interface
(keyword callers break). The interface analysis flags this separately,
so it is not hidden by local-rename normalization.

## Noise filtering

Deterministic rules. Each hidden unit keeps a reason and is shown in a
collapsed "Hidden" section with counts. Nothing is dropped silently.

| Reason | Rule |
|---|---|
| `comment` | equal ASTs, different comments |
| `formatting` | equal ASTs, same comments |
| `docstring` | equal after removing docstrings |
| `local-rename` | equal after canonical local renaming |
| `symbol-rename` | a removed and an added private symbol have equal normalized bodies |
| `move` | same symbol, same normalized body, different file |
| `imports` | only import order or grouping changed |
| `generated` | lockfiles (`uv.lock`, `poetry.lock`, …), files matching configured globs |

Renames or moves of **public** symbols are interface changes, not noise.
They appear in the Interfaces section as "renamed X → Y".

## Structural analysis

### Interfaces

Extracted at base and head with `ast`, then diffed:

- Public functions, classes and methods: no leading `_`, or listed in
  `__all__`. Before and after signatures, including defaults, keyword-
  only parameters and annotations.
- Fields of dataclasses, pydantic models, `TypedDict`s and SQLAlchemy
  models.
- HTTP routes: FastAPI/Flask decorators (method, path, handler).
- CLI definitions: click, typer, argparse (`add_argument` calls).
- Environment variables read (`os.environ[...]`, `os.getenv`,
  `os.environ.get`).
- Alembic migrations (any new file under the migrations folder).
- Dependencies in `pyproject.toml` (added, removed, version changes).

### Architecture

- Build the import graph at base and head with `ast`, limited to
  modules inside the repo. Resolve relative imports.
- Collapse to the deepest package level whose diagram has at most 25
  nodes. Packages can be expanded to module level in the UI.
- Diff the graphs: added and removed nodes and edges, plus packages
  that contain changed units. Direct neighbors of changed packages are
  added for context only while the diagram stays at 12 nodes or fewer,
  and only edges touching the change are drawn, so hub modules don't
  pull in the whole repo.
- Render as Mermaid: new edges green, removed red (dashed), changed
  packages highlighted.
- New third-party imports are listed next to the diagram.

### Signals

Units carry flags from this analysis (`public-signature`,
`model-field`, `route`, `cli`, `env`, `new-dependency`,
`new-module-edge`, …). The LLM sees them as hints, and the UI shows
them as badges.

## LLM ranking

Done by Claude inside the skill, not by the CLI.

**Input** (`analysis.json`): the PR title and description, the
architecture and interface diffs, and the non-noise units with **no
cached ranking**. Each unit includes its diff and signals.

**Output** (`ranking.json`), validated by `reviewer ingest`
(`reviewer/ranking.py`), which also rejects rankings that leave units
unranked:

```json
{
  "overview": "2–3 sentences on what the PR does.",
  "architecture_summary": "2–3 sentences, or null if no change.",
  "components": [
    {"name": "retry", "behavior": ["Retries on 429 as well as 5xx", "..."]}
  ],
  "units": [
    {
      "fingerprint": "…",
      "tier": "architecture | interface | behavior | detail",
      "reason": "why this tier, one line",
      "summary": "what changes in behavior, one line"
    }
  ]
}
```

**Caching:** rankings are stored by fingerprint. After a force-push,
only units with new fingerprints are sent to Claude, so re-runs are
cheap. The overview and component summaries are regenerated per run.

**Tiers:**

| Tier | Meaning |
|---|---|
| `architecture` | new or removed modules or packages, new dependencies between components, new third-party dependencies, changes to layering |
| `interface` | anything callers or clients depend on: public signatures, models, routes, CLI, env vars, schemas |
| `behavior` | logic a reviewer should understand: control flow, error handling, retries, concurrency, security-relevant code, data handling |
| `detail` | correct-looking local changes that don't change what the component does |

## Review state and force-pushes

When you mark a unit reviewed, the tool stores the unit's `qualname`,
`fingerprint` and the `new_body` you saw.

On every analysis, each current unit is matched against stored reviews:

| Match | Shown as |
|---|---|
| same `fingerprint` reviewed | ✓ seen, collapsed |
| same `qualname`, different fingerprint | ◐ changed since you reviewed, with a diff of the reviewed body against the current body, plus the full diff |
| no review | ● new |

This replaces git interdiff. It needs no old SHAs, which GitHub may
garbage-collect after a force-push, and rebase noise disappears because
it never changes a unit's normalized bodies.

Each unit can also be flagged (⚑) to come back to.

## Storage

SQLite (stdlib `sqlite3`) at `~/.local/share/reviewer/reviewer.db`
(override with `REVIEWER_DATA_DIR`). Chosen over LevelDB because it
ships with Python, needs no native library, and the data is relational
and worth querying.

```sql
prs        (id, repo, number, title, url, base_sha, head_sha, merge_base,
            analyzed_at, analysis)   -- analysis: JSON of architecture,
                                     -- interfaces and counts
units      (id, pr_id, path, old_path, qualname, old_qualname, kind,
            status, old_body, new_body, old_lines, new_lines,
            fingerprint, noise, signals)
rankings   (fingerprint PRIMARY KEY, tier, reason, summary, model,
            created_at)
reviews    (id, pr_id, qualname, fingerprint, reviewed_body, status,
            created_at)              -- status: reviewed | flagged
comments   (id, pr_id, qualname, fingerprint, line_offset, side, body,
            state, github_id, created_at)
                                     -- state: draft | posted | orphaned
pr_summary (pr_id, head_sha, overview, architecture_summary,
            components_json)
```

`units` is replaced on each analysis. `rankings`, `reviews` and
`comments` persist.

Repos are fetched into cached bare clones under `~/.cache/reviewer/`
(override with `REVIEWER_CACHE_DIR`; `git fetch origin
pull/<n>/head`), so your working checkout is never touched. The PR is
diffed from the merge base of its base and head, as GitHub does.

## Report UI

Plain HTML, CSS and JS in `reviewer/web/`, with diff2html and Mermaid
vendored so it works offline. Served by `reviewer serve` on
`127.0.0.1` with a random port; the command opens the browser.

```
┌ PR #123  Add retry policy to HTTP client      ● 3 new since your last review ┐
│ [Overview] [Architecture] [Interfaces] [Changes]       ▓▓▓▓▓░░░ 14/22 ✓  │
├──────────────────────────────────────────────────────────────────────────┤
│ Overview: 3-sentence PR summary + per-component behavior bullets         │
│                                                                          │
│ ▼ ARCHITECTURE (2)                                                       │
│   ● NEW  http/ → retry/   (new dependency)                     [✓] [💬]  │
│ ▼ INTERFACE (4)                                                          │
│   ◐ CHANGED SINCE REVIEW  Client.get(url, *, timeout) → (…, retry=None)  │
│        ▸ diff since you reviewed  ▸ full diff                            │
│ ▼ BEHAVIOR (7)                                                           │
│   ✓ RetryPolicy.run — now retries on 429; backoff cap 30s→60s            │
│ ▶ DETAIL (9)                                                             │
│ ▶ HIDDEN (31): 18 rename · 9 docstring · 4 formatting                    │
│                                                                          │
│ [Show only unreviewed]                     Drafts (3) [Submit review ▾]  │
└──────────────────────────────────────────────────────────────────────────┘
```

**Tabs:**

- **Overview:** PR summary, per-component behavior bullets, review
  progress.
- **Architecture:** the Mermaid graph diff, the summary, new
  third-party dependencies.
- **Interfaces:** a before/after table of signatures, model fields,
  routes, CLI options, env vars and dependencies.
- **Changes:** all units grouped by tier. Architecture, Interface and
  Behavior are expanded; Detail and Hidden are collapsed. Each unit
  shows its status, badges, summary, reason and diff.

**Interaction:**

- Syntax-highlighted diffs, split or unified.
- Click a line to start a comment. Comments are saved as drafts
  immediately.
- Filter: show only unreviewed or changed since review.
- Keyboard: `j`/`k` next and previous unit, `o` expand or collapse,
  `r` mark reviewed, `f` flag, `c` comment, `?` help.
- UI preferences (split or unified, collapsed sections) are kept in
  `localStorage`. All review state is kept in SQLite through the server.

### Server API

`reviewer/server.py`, built on stdlib `http.server`. Binds to
`127.0.0.1` only.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` and `/static/*` | the report |
| `GET` | `/api/pr` | PR, summary, graph, interfaces, units with rankings and review status |
| `PUT` | `/api/units/<qualname>/review` | mark reviewed, flag, or clear |
| `POST` | `/api/comments` | create a draft |
| `PATCH`, `DELETE` | `/api/comments/<id>` | edit or delete a draft |
| `POST` | `/api/review` | submit drafts as one GitHub review |

Every mutating request must carry a per-session token that the server
generates at startup and injects into the page. This keeps other local
web pages from posting reviews through the server.

## Posting to GitHub

`POST /api/review` with `event` = `COMMENT`, `APPROVE` or
`REQUEST_CHANGES` and an optional summary body:

1. Re-fetch the PR head SHA. If it moved since the last analysis, stop
   and ask you to re-run the analysis.
2. Map each draft to `path` and `line` in the current head:
   unit start line + `line_offset`.
3. Comments on lines inside the PR diff become inline review comments.
   Others (outside the diff, on architecture items, or on removed
   symbols) go into the review body as
   `On path/to/file.py \`Client.get\`: …`.
4. Call `gh api POST /repos/{owner}/{repo}/pulls/{n}/reviews` with
   `commit_id`, `event`, `body` and `comments`. Uses your existing `gh`
   login; the tool never handles a token.
5. Mark drafts `posted` and store their GitHub ids.

**Drafts across force-pushes:** drafts are anchored by `qualname` and
line offset. If the symbol still exists, the draft follows it. If it is
gone, the draft is marked `orphaned` and shown in the drafts panel
instead of being lost.

## CLI

```
reviewer analyze <pr> [--repo owner/name] [--out analysis.json]
reviewer ingest  <pr> <ranking.json>
reviewer serve   <pr> [--no-open]
reviewer status  [<pr>]          # PRs known locally, review progress
```

`<pr>` is a number (repo taken from the current directory's remote),
`owner/repo#n`, or a PR URL. GitHub Enterprise works through `gh`'s
host configuration.

## Skill

`skills/review-pr/SKILL.md`:

1. Resolve the PR from the argument or the current branch.
2. Run `reviewer analyze <pr> --out <scratch>/analysis.json`.
3. Read the analysis. Rank every unit in it and write the overview and
   component summaries, following the tier definitions above.
4. Write `ranking.json` and run `reviewer ingest`. On a validation
   error, fix and retry.
5. Run `reviewer serve <pr>` in the background and tell the user the
   URL, plus a short text summary: tiers and counts, new since last
   review.

For very large PRs, the skill ranks units in batches by package.

## Project layout

```
reviewer/
  cli.py            # analyze | ingest | serve | status
  github.py         # gh wrappers: PR metadata, cloning, post review
  repo.py           # cached bare clones, reading files at a SHA
  units.py          # ast → symbol units, normalization, fingerprints
  noise.py          # noise filtering rules
  interfaces.py     # signatures, models, routes, CLI, env vars, deps
  graph.py          # import graph, graph diff, Mermaid rendering
  analysis.py       # runs the above on a PR, builds the model's input
  ranking.py        # validates the model's ranking
  store.py          # SQLite schema and queries
  server.py         # stdlib HTTP server and JSON API
  web/              # index.html, app.js, styles.css, vendor/
skills/review-pr/SKILL.md
tests/
docs/design.md
```

Tooling: Python ≥ 3.12, `uv`, `pytest`. Runtime needs `gh` and `git`
on PATH. No third-party runtime dependencies.

## Testing

Each scenario is a small base/head pair, written inline in the tests,
with the expected units, noise reasons, structural signals and
fingerprint behavior. Pipeline tests build a real temporary git
repository and stub out `gh`:

- docstring-only, comment-only and formatting-only changes are hidden
- local rename hidden; public parameter rename flagged as interface
- private symbol rename hidden; public symbol rename flagged
- move between files detected
- rebase onto a new base with no PR changes keeps all fingerprints
- force-push changing one function marks only that unit changed
- new package dependency appears in the graph diff
- route, model field, env var and CLI changes are detected

The server and GitHub posting are tested with `gh` stubbed out. The
comment-to-line mapping gets its own unit tests, including lines
outside the diff.

## Milestones

1. **Core analysis:** `units.py`, `noise.py`, fingerprints, fixture
   tests.
2. **Structure:** `interfaces.py`, `graph.py`.
3. **Pipeline:** `github.py`, `repo.py`, `store.py`, `analyze` and
   `ingest`, the skill.
4. **Report:** `server.py` and the web UI with review state.
5. **Comments and posting:** drafts, line mapping, submitting reviews.

## Open questions

- Should `detail`-tier units count toward review progress, or only
  the top three tiers?
- Very large PRs: is batching by package enough, or do we need a
  per-run cap on units sent to Claude?

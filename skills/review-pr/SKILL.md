---
name: review-pr
description: Review a GitHub pull request by what matters - architecture, interfaces and behavior - with renames, comments, docstrings and formatting filtered out, and review state that survives force-pushes. Use when the user asks to review a PR, look at a PR, or see what changed in a PR since they last reviewed it. Takes a PR number, owner/repo#n or URL; defaults to the current branch's PR. Python repositories only.
---

# Review a pull request

The `reviewer` CLI does the deterministic work: it splits the PR into
symbol-level units (functions, methods, class bodies, module code),
hides noise, detects interface and architecture changes, and remembers
what was already ranked. Your job is the judgement: rank each unit
that is left and summarize the PR.

## Prerequisites

`reviewer`, `gh` and `git` must be on PATH, and `gh` must be logged in.
If `reviewer` is missing, tell the user to install it from the
ai-reviewer checkout with `uv tool install --editable <path>` and stop.

## Steps

1. **Analyze.** Make a scratch directory and run:

   ```bash
   reviewer analyze <pr> --out <scratch>/analysis.json
   ```

   Omit `<pr>` to use the current branch's PR. Pass `--repo
   [host/]owner/name` when `<pr>` is a bare number and the current
   directory is not a checkout of that repository. The command prints
   a one-line summary on stderr.

2. **Read the analysis.** `analysis.json` has:
   - `pr`: title, description (`body`), author, refs and SHAs.
   - `architecture`: the import-graph diff (`nodes`, `edges`,
     `external_added`, `external_removed`, `mermaid`).
   - `interfaces`: changes to public signatures, model fields, routes,
     CLI, environment variables, dependencies and migrations, with
     before and after values.
   - `counts`: units in total, hidden noise by reason, already ranked,
     and to rank.
   - `units`: only the units you must rank. Each has a `fingerprint`,
     `qualname`, `kind`, `status`, `signals` and a unified `diff`.

   If `units` has more than about 60 entries, read it in parts, for
   example one package at a time with `jq '.units[] | select(.path |
   startswith("pkg/"))'`.

   If `units` is empty, every visible unit is already ranked: still
   write the overview and components (step 3) with an empty `units`
   list, so the summary matches the current head.

3. **Rank.** Write `<scratch>/ranking.json`:

   ```json
   {
     "overview": "2-3 sentences: what the PR does and why.",
     "architecture_summary": "2-3 sentences on how the structure changes, or null.",
     "components": [
       {"name": "http client", "behavior": ["fetch() retries 429 and 5xx up to 3 times"]}
     ],
     "units": [
       {
         "fingerprint": "<copied exactly from the analysis>",
         "tier": "architecture | interface | behavior | detail",
         "reason": "why this tier, one line",
         "summary": "what changes, one line"
       }
     ]
   }
   ```

   Every unit in the analysis must appear exactly once.

4. **Store it.**

   ```bash
   reviewer ingest <pr> <scratch>/ranking.json --model <your model id>
   ```

   Use the same `<pr>` and `--repo` as in step 1. If it reports
   validation errors, fix the file and run it again.

5. **Open the report.** Run `reviewer serve <pr>` (same `<pr>` and
   `--repo`) in the background. It prints the report's URL and opens
   it in the browser. There the user marks units reviewed and flags
   them; this state survives force-pushes.

6. **Report to the user.** Give the report URL and a short summary in
   the terminal:
   - the overview,
   - the architecture summary, if any, and new third-party
     dependencies,
   - interface changes worth knowing, one line each,
   - the units in the `architecture`, `interface` and `behavior` tiers,
     one line each with their summary, most important first,
   - counts: how many units were detail, and how many were hidden as
     noise and why (from `counts.hidden`).

   Don't list detail-tier units or hidden units individually unless the
   user asks.

## Choosing a tier

Pick the highest tier that fits. The `signals` are hints from static
analysis, not verdicts.

| Tier | Use for | Typical signals |
|---|---|---|
| `architecture` | New or removed modules or packages, new dependencies between components, new or removed third-party dependencies, changes to layering or who owns what | `new-module-edge`, `removed-module-edge`, `new-dependency`, `dependency` (when added or removed) |
| `interface` | Anything callers or clients depend on: public signatures, data models, routes, CLI, env vars, config keys, schemas, migrations | `public-signature`, `model-field`, `route`, `cli`, `env`, `migration` |
| `behavior` | Logic a reviewer must understand: control flow, error handling, retries, timeouts, concurrency, caching, security-relevant code, data handling, changed defaults | none needed |
| `detail` | Local changes that don't change what the component does: small refactors, logging, type annotations only, test-only helpers, dependency version bumps within a major version | none |

Guidance:

- A `public-signature` signal whose only change is an added type
  annotation or a new optional keyword argument at the end can be
  `detail`. A removed or renamed parameter, a changed default or a
  changed return type is `interface`.
- Tests are usually `detail`, unless the test change reveals a behavior
  change the code diff hides, such as an updated expected value.
- A module-level unit (`kind: module`) that only adds imports gets the
  tier of what those imports enable: `architecture` if it creates a new
  component dependency, otherwise `detail`.
- An added unit is ranked by what it introduces, a removed unit by what
  disappears.

## Writing summaries

- `summary` states the change in behavior, concretely, in at most about
  120 characters: "Retries on 429 as well as 5xx; backoff cap 30s→60s",
  not "Updated retry logic".
- Don't restate the symbol name; the report shows it next to the
  summary.
- `reason` says why the tier applies: "callers passing timeout
  positionally break".
- `components` group behavior by feature or package. Only behavior a
  reviewer should know goes there, not implementation details.
- Stick to what the diff shows. If intent is unclear, say so ("unclear
  why the lock was removed") instead of guessing.

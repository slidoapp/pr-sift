---
name: pr-sift
description: Review a GitHub pull request by what matters - architecture, interfaces and behavior - with renames, comments, docstrings and formatting filtered out, and review state that survives force-pushes. Use when the user asks to review a PR, look at a PR, or see what changed in a PR since they last reviewed it. Takes a PR number, owner/repo#n or URL; defaults to the current branch's PR. Python repositories only.
---

# Review a pull request

The `pr-sift` CLI does the deterministic work: it splits the PR into
symbol-level units (functions, methods, class bodies, module code),
hides noise, detects interface and architecture changes, and remembers
what was already ranked. Your job is the judgement: rank each unit
that is left and summarize the PR.

## Prerequisites

`pr-sift`, `gh` and `git` must be on PATH, and `gh` must be logged in.
If `pr-sift` is missing, tell the user to install it with
`uv tool install git+https://github.com/slidoapp/pr-sift` and stop.

## Steps

1. **Analyze.** Make a scratch directory and run:

   ```bash
   pr-sift analyze <pr> --out <scratch>/analysis.json
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
     `qualname`, `kind`, `status`, `signals`, a unified `diff` (hunk
     headers use file line numbers) and `folds`: the line ranges the
     report collapses into tables, such as long lists of cases or a
     JSON fixture.
   - `outline`: every visible unit, including ones ranked before, with
     `uses` and `used_by` (which changed units it names, and which name
     it) and its cached `ranking`, if any. Use it for the walkthrough.

   If `units` has more than about 60 entries, read it in parts, for
   example one package at a time with `jq '.units[] | select(.path |
   startswith("pkg/"))'`.

   If `units` is empty, every visible unit is already ranked: still
   write the overview, components and walkthrough (step 3) with an
   empty `units` list, so they match the current head. If the user asks to
   rank again, rerun step 1 with `--rerank`.

3. **Rank.** Write `<scratch>/ranking.json`:

   ```json
   {
     "overview": "2-3 sentences: what the PR does and why.",
     "architecture_summary": "2-3 sentences on how the structure changes, or null.",
     "components": [
       {"name": "http client", "behavior": ["fetch() retries 429 and 5xx up to 3 times"]}
     ],
     "walkthrough": [
       {"type": "text", "text": "### Retries for flaky requests\n\nWhat and why, in prose."},
       {"type": "architecture"},
       {"type": "code", "unit": "app.client.fetch"},
       {"type": "code", "unit": "app.retry.with_retry", "lines": [12, 30]},
       {"type": "units", "title": "Tests", "units": ["tests.test_retry.test_gives_up"]}
     ],
     "units": [
       {
         "fingerprint": "<copied exactly from the analysis>",
         "tier": "architecture | interface | behavior | detail",
         "reason": "why this tier, one line",
         "summary": "what changes, one line",
         "description": "what the symbol does, one line, or null",
         "attention": "read | skim | hide",
         "highlights": [{"line": 312, "note": "why this line deserves a look"}],
         "sections": [{"line": 300, "title": "Load the segments and their metadata"}]
       }
     ]
   }
   ```

   Every unit in the analysis must appear exactly once. Write a
   `description` only for units with interface signals
   (`public-signature`, `model-field`, `route`, `cli`, `env`) whose
   docstring is missing or doesn't say what they do; otherwise use
   `null`. The report shows the docstring's first line instead when
   there is one. See "Pre-filtering" below for `attention` and
   `highlights`.

4. **Store it.**

   ```bash
   pr-sift ingest <pr> <scratch>/ranking.json --model <your model id>
   ```

   Use the same `<pr>` and `--repo` as in step 1. If it reports
   validation errors, fix the file and run it again.

5. **Open the report.** Run `pr-sift serve <pr>` (same `<pr>` and
   `--repo`) in the background. It prints the report's URL and opens
   it in the browser. There the user marks units reviewed and flags
   them, drafts comments and posts them as a GitHub review, and asks
   Claude about any line, unit or walkthrough paragraph; this state
   survives force-pushes. Questions
   are answered by a separate headless Claude Code run, not by you.

6. **Report to the user.** Give the report URL and a short summary in
   the terminal:
   - the overview,
   - the architecture summary, if any, and new third-party
     dependencies,
   - interface changes worth knowing, one line each,
   - the units to read in the `architecture`, `interface` and
     `behavior` tiers, one line each with their summary, most important
     first,
   - the highlights, one line each,
   - counts: how many units are detail, how many to skim, and how many
     were hidden as noise and why (from `counts.hidden`).

   Don't list detail-tier, skim or hidden units individually unless the
   user asks.

## Writing the walkthrough

The walkthrough is the report's first tab: the story of the change, read
top-down, with code only where the story needs it. Write it after
ranking, for the whole PR (`outline`), not only the units you ranked
this time.

Blocks:

- `text`: prose. Paragraphs are separated by blank lines; `### ` starts
  a heading, `- ` a list item; `**bold**`, backticks and
  `[links](https://…)` work.
- `code`: a unit's code, by `qualname` from `outline`. Without `lines`
  the report shows the unit's key lines (its skeleton and highlights);
  with `lines: [first, last]` (file lines) only that part, for a long
  unit where one section matters.
- `units`: a titled list of units that need no code here, each shown
  with its summary.
- `architecture`: the import-graph diagram. Include it when
  `architecture.edges` or `external_added`/`external_removed` show a
  structural change; leave it out otherwise.

Structure, top-down, and only as deep as needed:

1. **What and why**, 2–3 sentences, from the PR description and code.
2. **The shape**, when components or their connections change: which
   ones and how, with the `architecture` block.
3. **What callers see**, when interfaces change.
4. **The walkthrough**: follow the main path through the change, from
   the entry points (units with empty `used_by`) to what they use. One
   short step per paragraph, each followed by the `code` block it talks
   about. Mention what each highlight is about in the prose next to it.
5. **Also in this PR**: `units` blocks grouping what needs no code here
   (tests, fixtures, docs, small helpers), by purpose.
6. **Before merging**, when there is something: the open risks and to-dos
   in one list (highlights, TODO pins and the like from the description).

How deep to go follows attention: every `read` unit (and any unit with
highlights) gets a `code` block; every `skim` unit is in a `units`
block or, if the story needs it, a `code` block; `hide` and noise units
are left out or listed at most. `pr-sift ingest` rejects a walkthrough
that leaves out a `read` or `skim` unit. Keep the prose short: a step is
1–3 sentences. Say what the code does and why it matters; don't narrate
the code line by line, and don't repeat the summaries.

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

## Pre-filtering

The reviewer wants to read only what can hide a mistake. Everything
else should get out of the way.

**`attention`.** Use `hide` for units with nothing to review, and say
why in `reason`: trivial constants and aliases (`EVENT = {"id": TEST_ID}`),
test input text, values only other reviewed code gives meaning to.
Import changes that add no dependency are hidden by a rule, so they
don't reach you.

Use `skim` for units a reviewer doesn't need to read line by line, and
say why in `reason`:

- repetitive code, such as many similar test cases, fixtures and request
  payloads,
- trivial helpers whose mistakes the tests or the first run would catch
  at once,
- tests that only add coverage, unless an assertion or expected value
  changed in a way that reveals a behavior change,
- generated-looking or boilerplate code.

Keep `read` (the default) for anything with real logic, error handling,
data writes, security, concurrency or interface changes, even when it's
short. When unsure, use `read`. A unit with highlights is always shown
as `read`, whatever its `attention`: a line worth a look must not be
skimmed past.

**`highlights`.** For any unit, read or skim, list the few lines a
reviewer should look at even if they read nothing else, especially
inside `folds`, which the report collapses: magic numbers and special
ids, writes or deletes against real systems, sleeps, timeouts and
retries, changed limits and defaults, environment-specific values,
order dependencies. Use the file line from the diff, and point at the line that holds the
code the note is about, never at a line of only brackets (`}`, `),`).
Most units need none; don't highlight lines just to have some. Notes
say what to check, in at most about 100 characters: "max int32 as a
fake id written to the production index".

**`sections`.** A long unit is easier to review as a few titled
blocks. For any `read` unit longer than about 30 lines, or with several
distinct steps, split it into 2–6 sections at natural boundaries (blank
lines, or the top-level statements of the function body) and give each
a title like a short code comment: "Place segments on the timeline,
pushing overlaps back". `line` is the file line of the block's first
statement, from the diff; the report shows the title before it and
always shows that line. Titles say what the block does, in at most
about 60 characters, with code in backticks. Short units need no
sections.

## Writing summaries

- Put code in backticks everywhere (overview, components, reasons,
  summaries, descriptions): identifiers, paths, endpoints, values and
  expressions, such as `fetch()`, `retries=3`, `/shorten` or
  `dags/storage/`. The report renders them as inline code.
- `summary` states the change in behavior, concretely, in at most about
  120 characters: "Retries on `429` as well as `5xx`; backoff cap
  30s→60s", not "Updated retry logic".
- `description` says what the symbol is for, not what changed: "POSTs
  each smoke case and fails the task once at the end if any failed".
- Don't restate the symbol name; the report shows it next to the
  summary.
- `reason` says why the tier applies: "callers passing timeout
  positionally break".
- `components` group behavior by feature or package. Only behavior a
  reviewer should know goes there, not implementation details.
- Stick to what the diff shows. If intent is unclear, say so ("unclear
  why the lock was removed") instead of guessing.

# ai-reviewer

Review GitHub pull requests by what matters: architecture, interfaces
and behavior. Renames, comments, docstrings and formatting are filtered
out, and what you reviewed is remembered across force-pushes. From
the report you can draft comments and ask Claude about any line, unit
or paragraph. Python repositories only, for now.

See [docs/design.md](docs/design.md) for how it works.

## Install

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), `git`, and
the GitHub CLI `gh`, logged in.

```bash
uv tool install --editable .
```

Skills live in `~/.agents/skills`, with a link per skill from each
agent's skills folder:

```bash
mkdir -p ~/.agents/skills ~/.claude/skills ~/.codex/skills
ln -s "$PWD/skills/review-pr" ~/.agents/skills/review-pr
ln -s ../../.agents/skills/review-pr ~/.claude/skills/review-pr
ln -s ../../.agents/skills/review-pr ~/.codex/skills/review-pr
```

Both installs point at this checkout, so changes here take effect
without reinstalling.

## Use

In Claude Code, inside any checkout of the repository:

```
/review-pr 123
```

Or run the analysis yourself:

```bash
reviewer analyze 123 --out analysis.json   # or owner/repo#123, or a PR URL
reviewer serve 123                         # open the report in the browser
reviewer status
```

In the report, hover a line and click **+**, or select lines or
walkthrough text, to write a comment draft or ask Claude about it
(keys `c` and `a`). Questions run `claude -p` in a checkout of the PR
head, with read-only file tools and web search, using your Claude Code
login and default model. Questions are private; drafts stay local
until posting lands.

Data lives in `~/.local/share/reviewer` and repository clones in
`~/.cache/reviewer`; set `REVIEWER_DATA_DIR` and `REVIEWER_CACHE_DIR`
to move them.

Optional settings go in `~/.config/reviewer/config.toml`
(`REVIEWER_CONFIG_DIR` moves it):

```toml
[agent]
command = "claude"            # the agent CLI that answers questions
web_fetch = true              # false: it can search the web but not open pages
web_fetch_domains = []        # e.g. ["docs.python.org", "pypi.org"] to allow only these
max_parallel = 3
timeout_seconds = 300
```

## Develop

```bash
uv sync
uv run pytest
```

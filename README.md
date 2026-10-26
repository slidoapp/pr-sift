# ai-reviewer

Review GitHub pull requests by what matters: architecture, interfaces
and behavior. Renames, comments, docstrings and formatting are filtered
out, and what you reviewed is remembered across force-pushes. Python
repositories only, for now.

See [docs/design.md](docs/design.md) for how it works.

## Install

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), `git`, and
the GitHub CLI `gh`, logged in.

```bash
uv tool install --editable .
mkdir -p ~/.claude/skills
ln -s "$PWD/skills/review-pr" ~/.claude/skills/review-pr
```

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

Data lives in `~/.local/share/reviewer` and repository clones in
`~/.cache/reviewer`; set `REVIEWER_DATA_DIR` and `REVIEWER_CACHE_DIR`
to move them.

## Develop

```bash
uv sync
uv run pytest
```

"""Installing the pr-sift skill for coding agents.

The skill ships in the package, so upgrading pr-sift upgrades it too.
It is linked into ~/.agents/skills, and from there into the skills
folder of each agent found.
"""

from pathlib import Path

SKILL_DIR = Path(__file__).parent / "skill"
NAME = "pr-sift"
AGENT_DIRS = (".claude", ".codex")


class SkillError(RuntimeError):
    pass


def install(home: Path | None = None) -> list[Path]:
    """Link the skill for every agent found under home; returns the links."""
    home = home or Path.home()
    shared = home / ".agents/skills" / NAME
    links = {shared: SKILL_DIR}
    for agent in AGENT_DIRS:
        if (home / agent).is_dir():
            links[home / agent / "skills" / NAME] = Path("../../.agents/skills") / NAME
    # Links are ours to replace; anything else is the user's.
    for path in links:
        if path.exists() and not path.is_symlink():
            raise SkillError(f"{path} exists and is not a link: move it away and run this again")
    for path, target in links.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        path.symlink_to(target)
    return list(links)

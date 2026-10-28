import pytest

from pr_sift import skills


def test_the_skill_is_linked_for_each_agent_found(tmp_path):
    (tmp_path / ".claude").mkdir()
    links = skills.install(tmp_path)
    shared = tmp_path / ".agents/skills/pr-sift"
    assert links == [shared, tmp_path / ".claude/skills/pr-sift"]
    assert shared.readlink() == skills.SKILL_DIR
    assert (tmp_path / ".claude/skills/pr-sift").readlink().as_posix() == "../../.agents/skills/pr-sift"
    assert (tmp_path / ".claude/skills/pr-sift/SKILL.md").read_text().startswith("---\nname: pr-sift\n")
    assert not (tmp_path / ".codex").exists()


def test_old_links_are_replaced_and_anything_else_kept(tmp_path):
    (tmp_path / ".codex/skills").mkdir(parents=True)
    old = tmp_path / ".agents/skills/pr-sift"
    old.parent.mkdir(parents=True)
    old.symlink_to(tmp_path / "old-checkout/skills/pr-sift")  # dangling
    skills.install(tmp_path)
    assert old.readlink() == skills.SKILL_DIR

    mine = tmp_path / ".codex/skills/pr-sift"
    mine.unlink()
    mine.mkdir()
    with pytest.raises(skills.SkillError, match="is not a link"):
        skills.install(tmp_path)
    assert mine.is_dir() and not mine.is_symlink()

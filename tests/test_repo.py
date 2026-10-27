from pr_sift.repo import changed_paths, list_files, merge_base, read_files


def test_changed_paths_and_read_files(git_repo):
    base = git_repo.commit({"a.py": "A = 1\n", "b.py": "B = 1\n", "old.txt": "x\n"})
    git_repo.git("switch", "--quiet", "-c", "feature")
    head = git_repo.commit({"a.py": "A = 2\n", "old.txt": None, "new dir/c.py": "C = 1\n", "logo.png": b"\x89PNG\0\0"})

    old, new = changed_paths(git_repo.git_dir, base, head)
    assert sorted(old) == ["a.py", "old.txt"]
    assert sorted(new) == ["a.py", "logo.png", "new dir/c.py"]

    assert read_files(git_repo.git_dir, head, ["a.py", "logo.png", "missing.py", "new dir/c.py"]) == {
        "a.py": "A = 2\n",
        "new dir/c.py": "C = 1\n",
    }
    assert read_files(git_repo.git_dir, head, []) == {}
    assert sorted(list_files(git_repo.git_dir, head)) == ["a.py", "b.py", "logo.png", "new dir/c.py"]


def test_merge_base(git_repo):
    base = git_repo.commit({"a.py": "A = 1\n"})
    git_repo.git("switch", "--quiet", "-c", "feature")
    head = git_repo.commit({"a.py": "A = 2\n"})
    git_repo.git("switch", "--quiet", "main")
    main = git_repo.commit({"b.py": "B = 1\n"})
    assert merge_base(git_repo.git_dir, main, head) == base

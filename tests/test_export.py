import json
import re

from pr_sift.anchors import make_anchor
from pr_sift.cli import main
from pr_sift.export import static_page
from pr_sift.noise import classify
from pr_sift.store import Store
from pr_sift.units import diff_files

BASE = {"pkg/mod.py": "def f():\n    return 1\n"}
HEAD = {"pkg/mod.py": "def f():\n    return '</script>'\n"}


def analyzed(store):
    units = classify(diff_files(BASE, HEAD))
    return store.save_analysis("github.com/o/r", 1, {"title": "T", "head_sha": "abc"}, units, {"architecture": None})


def embedded(page):
    return json.loads(re.search(r'<script type="application/json" id="pr-sift-data">(.*?)</script>', page, re.S)[1])


def test_the_page_carries_its_data_and_assets():
    store = Store()
    page = static_page(store, analyzed(store))

    assert "/static/" not in page
    assert "__TOKEN__" not in page
    # One opening and one closing tag per script element: the inlined code
    # and data can't end one early or look like another.
    assert page.count("</script>") == page.count("<script") == len(re.findall(r"<script[ >]", page)) == 4
    # At the end of the head, though mermaid's code holds a "</head>" too.
    assert re.search(r'id="pr-sift-data">[^<]*</script>\n</head>\n<body>', page)
    data = embedded(page)
    assert data["static"] is True
    assert data["agent"]["available"] is False
    assert [u["qualname"] for u in data["units"]] == ["pkg.mod.f"]
    assert "'</script>'" in json.dumps(data["units"][0]["diff"])


def test_the_copy_leaves_out_the_reviewers_own_state():
    store = Store()
    pr_id = analyzed(store)
    unit = store.units(pr_id)[0]
    for status in ("reviewed", "flagged"):
        store.set_review(pr_id, unit["qualname"], unit["fingerprint"], unit["new_body"], status)
    store.add_comment(pr_id, make_anchor({"kind": "pr"}, store.units(pr_id), []), "my draft")
    question = store.add_question(pr_id, make_anchor({"kind": "pr"}, store.units(pr_id), []))
    store.add_message(question, "you", "my question")

    page = static_page(store, pr_id)

    data = embedded(page)
    assert (data["units"][0]["state"], data["units"][0]["flagged"]) == ("new", False)
    assert data["has_reviews"] is False
    assert (data["comments"], data["questions"], data["submissions"]) == ([], [], [])
    assert "my draft" not in page and "my question" not in page


def test_export_command_writes_the_file(tmp_path, capsys):
    analyzed(Store())
    out = tmp_path / "report.html"

    assert main(["export", "o/r#1", "--out", str(out)]) == 0
    assert embedded(out.read_text())["pr"]["number"] == 1
    assert main(["export", "o/r#2", "--out", str(out)]) == 1
    assert "has not been analyzed" in capsys.readouterr().err

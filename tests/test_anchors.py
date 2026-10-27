import json

import pytest

from pr_sift.anchors import AnchorError, block_fingerprint, make_anchor, resolve
from pr_sift.noise import classify
from pr_sift.store import Store
from pr_sift.units import diff_files

BASE = {"pkg/mod.py": "def f():\n    return 1\n\n\ndef g():\n    x = 1\n    return x\n"}
HEAD = {"pkg/mod.py": "def f():\n    return 2\n\n\ndef g():\n    x = 2\n    return x\n"}


def analyze(store, head):
    return store.save_analysis("github.com/o/r", 1, {"title": "T"}, classify(diff_files(BASE, head)), {})


def unit(store, pr_id, qualname):
    return next(u for u in store.units(pr_id) if u["qualname"] == qualname)


def line_of(store, pr_id, qualname, index, side="new"):
    return json.loads(unit(store, pr_id, qualname)[f"{side}_lines"])[index]


def test_line_anchor_follows_its_unit_when_code_above_moves():
    store = Store()
    pr_id = analyze(store, HEAD)
    g_return = line_of(store, pr_id, "pkg.mod.g", 2)
    anchor = make_anchor({"kind": "line", "qualname": "pkg.mod.g", "side": "new", "line": g_return}, store.units(pr_id), [])
    assert (anchor["side"], anchor["start_offset"]) == ("new", 2)
    store.add_comment(pr_id, anchor, "why?")

    # A force-push adds lines above g; g itself is unchanged.
    moved = {"pkg/mod.py": "X = 1\nY = 2\n\n\n" + HEAD["pkg/mod.py"]}
    pr_id = analyze(store, moved)
    [row] = store.comments(pr_id)
    view = resolve(row, store.units(pr_id), [])
    assert view == {
        "kind": "line", "qualname": "pkg.mod.g", "quote": None, "state": "current",
        "side": "new", "line": g_return + 4, "end_side": "new", "end_line": g_return + 4,
    }


def test_anchors_go_stale_or_orphaned():
    store = Store()
    pr_id = analyze(store, HEAD)
    units = store.units(pr_id)
    f_line = line_of(store, pr_id, "pkg.mod.f", 1)
    line = make_anchor({"kind": "range", "qualname": "pkg.mod.f", "side": "new", "line": f_line - 1,
                        "end_side": "new", "end_line": f_line}, units, [])
    whole = make_anchor({"kind": "unit", "qualname": "pkg.mod.g"}, units, [])
    for anchor in (line, whole):
        store.add_question(pr_id, anchor)

    # f changes again and g is no longer part of the PR.
    head = {"pkg/mod.py": "def f():\n    return 3\n\n\ndef g():\n    x = 1\n    return x\n"}
    pr_id = analyze(store, head)
    views = [resolve(q, store.units(pr_id), []) for q in store.questions(pr_id)]
    assert [(v["kind"], v["state"], v.get("line"), v.get("end_line")) for v in views] == [
        ("range", "stale", f_line - 1, f_line),
        ("unit", "orphaned", None, None),
    ]


def test_walkthrough_anchor_goes_stale_when_its_block_changes():
    store = Store()
    pr_id = analyze(store, HEAD)
    blocks = [{"type": "text", "text": "Intro"}, {"type": "text", "text": "More"}]
    anchor = make_anchor({"kind": "walkthrough", "block": 1, "quote": "  More  "}, [], blocks)
    assert (anchor["block"], anchor["quote"], anchor["fingerprint"]) == (1, "More", block_fingerprint(blocks[1]))
    # The report marks code blocks stale; that is no change to the block.
    assert resolve(anchor, [], [blocks[0], blocks[1] | {"stale": False}])["state"] == "current"
    assert resolve(anchor, [], [blocks[0], {"type": "text", "text": "Rewritten"}])["state"] == "stale"
    assert resolve(anchor, [], [blocks[0]])["state"] == "stale"


@pytest.mark.parametrize("data, error", [
    ({"kind": "nope"}, "anchor kind"),
    ({"kind": "unit", "qualname": "pkg.mod.zzz"}, "no unit"),
    ({"kind": "line", "qualname": "pkg.mod.f", "side": "new", "line": 999}, "not on the new side"),
    ({"kind": "line", "qualname": "pkg.mod.f", "side": "left", "line": 1}, "side must be"),
    ({"kind": "walkthrough", "block": 5}, "no such walkthrough block"),
])
def test_invalid_anchors_are_rejected(data, error):
    store = Store()
    pr_id = analyze(store, HEAD)
    with pytest.raises(AnchorError, match=error):
        make_anchor(data, store.units(pr_id), [{"type": "text", "text": "x"}])

from textwrap import dedent

from pr_sift.skeleton import skeleton_lines

RUNNER = '''\
def run(service_url, cases, timeout=30):
    """Run cases in order."""
    failures = []
    for case in cases:
        req_url = f"{service_url}{case.path}"
        logging.info(f"Url {req_url}")
        if case.wait_seconds:
            time.sleep(case.wait_seconds)
        try:
            response = requests.post(req_url, json=case.payload, timeout=timeout)
            logger.debug("status %s", response.status_code)
            assert response.status_code == 200
            case.validate(response.json())
        except Exception as e:
            print(e)
            failures.append(f"{case.name}: {e!r}")

    if failures:
        raise AssertionError("\\n".join(failures))
'''


def kept(source, changed=None, top_level=False, first_line=1):
    body = dedent(source)
    lines = list(range(first_line, first_line + len(body.splitlines())))
    changed = set(lines) if changed is None else changed
    return sorted(skeleton_lines(body, lines, changed, top_level))


def test_runner_skeleton_keeps_what_decisions_read_and_drops_logs():
    text = RUNNER.splitlines()
    assert [text[n - 1].strip() for n in kept(RUNNER)] == [
        "def run(service_url, cases, timeout=30):",
        "failures = []",
        "for case in cases:",
        "if case.wait_seconds:",
        "time.sleep(case.wait_seconds)",
        "try:",
        "response = requests.post(req_url, json=case.payload, timeout=timeout)",
        "assert response.status_code == 200",
        "case.validate(response.json())",
        "except Exception as e:",
        'failures.append(f"{case.name}: {e!r}")',
        "if failures:",
        'raise AssertionError("\\n".join(failures))',
    ]


def test_only_changed_statements_and_blocks_holding_changes():
    # Only the sleep line changed: the signature and its enclosing headers show.
    assert kept(RUNNER, changed={8}) == [1, 4, 7, 8]


def test_lines_map_to_file_lines():
    assert kept("def f():\n    return 1\n", first_line=40) == [40, 41]


def test_else_elif_finally_and_multiline_headers():
    source = '''\
    def f(a,
          b):
        if a:
            x = compute(a)
        elif b:
            return b
        else:
            raise ValueError()
        try:
            go()
        finally:
            stop()
    '''
    text = dedent(source).splitlines()
    assert [text[n - 1].strip() for n in kept(source)] == [
        "def f(a,", "b):", "if a:", "x = compute(a)", "elif b:", "return b", "else:", "raise ValueError()",
        "try:", "go()", "finally:", "stop()",
    ]


def test_statements_show_without_bracket_lines():
    source = "def f():\n    return {\n        k: {\n            'v': v,\n        }\n        for k, v in items\n    }\n"
    text = dedent(source).splitlines()
    assert [text[n - 1].strip() for n in kept(source)] == ["def f():", "return {", "k: {", "'v': v,", "for k, v in items"]


def test_long_statements_show_their_first_line():
    source = "def f():\n    call(\n" + "".join(f"        a{i}={i},\n" for i in range(16)) + "    )\n"
    assert kept(source) == [1, 2]


def test_top_level_statements_count_as_key():
    assert kept("LIMIT = 30\n", top_level=True) == [1]
    assert kept("LIMIT = 30\n") == []


def test_unparsable_code_has_no_skeleton():
    assert kept("def f(:\n") == []


def test_assignments_feeding_a_return_are_kept():
    source = "def f(items):\n    total = 0\n    label = 'x'\n    for item in items:\n        total += item\n    return total\n"
    text = dedent(source).splitlines()
    assert [text[n - 1].strip() for n in kept(source)] == [
        "def f(items):", "total = 0", "for item in items:", "total += item", "return total",
    ]


def test_long_statements_show_their_changed_lines():
    source = "def f():\n    call(\n" + "".join(f"        a{i}={i},\n" for i in range(16)) + "    )\n"
    assert kept(source, changed={5}) == [1, 2, 5]


def test_only_touched_nested_blocks_and_clauses_show():
    source = """\
async def run(
    self,
    a,
    b,
):
    try:
        async def process(x):
            return x
        result = Result(
            a=1,
            b=2,
            c=3,
            d=4,
            e=5,
            f=6,
        )
    finally:
        await close()
"""
    text = source.splitlines()
    changed = {12}  # c=3
    assert [text[n - 1].strip() for n in kept(source, changed=changed)] == [
        "async def run(", "try:", "result = Result(", "c=3,",
    ]

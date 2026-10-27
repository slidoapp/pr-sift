import pytest

from pr_sift.github import GitHubError, PrRef, parse_pr, parse_repo


def current():
    return "github.com", "me", "here"


@pytest.mark.parametrize(
    "spec, repo, expected",
    [
        ("https://github.com/o/r/pull/12", None, PrRef("github.com", "o", "r", 12)),
        ("https://ghe.corp/o/r/pull/12/files", None, PrRef("ghe.corp", "o", "r", 12)),
        ("o/r#7", None, PrRef("github.com", "o", "r", 7)),
        ("ghe.corp/o/r#7", None, PrRef("ghe.corp", "o", "r", 7)),
        ("5", None, PrRef("github.com", "me", "here", 5)),
        ("#5", "ghe.corp/o/r", PrRef("ghe.corp", "o", "r", 5)),
    ],
)
def test_parse_pr(spec, repo, expected):
    assert parse_pr(spec, repo, current) == expected


def test_parse_pr_rejects_garbage():
    with pytest.raises(GitHubError):
        parse_pr("main", None, current)


def test_parse_repo():
    assert parse_repo("https://ghe.corp/o/r.git") == ("ghe.corp", "o", "r")
    with pytest.raises(GitHubError):
        parse_repo("r")


def test_pr_ref():
    pr = PrRef("ghe.corp", "o", "r", 3)
    assert (pr.repo_arg, str(pr)) == ("ghe.corp/o/r", "o/r#3")

"""`ci-select pr N`: the comment body and the create-or-edit logic, offline."""

import json

from ci_selector.pr_comment import MARKER, PrSelection, StepView, post, render


def _selection(**over):
    fields = dict(
        pr=1,
        title="t",
        base="b" * 40,
        head="h" * 40,
        files=2,
        today=[StepView("a", 1), StepView("b", 2), StepView("c", 1, "amd")],
        selector=[StepView("a", 1), StepView("d", 3), StepView("c", 1, "amd")],
        plumbing=8,
        added_by={("d", ""): "Python record"},
        records=["Python record: build 1 at `abc`", "kernel record: x"],
    )
    fields.update(over)
    return PrSelection(**fields)


def test_the_body_carries_the_marker_and_the_counts():
    body = render(_selection())
    assert body.startswith(MARKER), "the marker is how --post finds its own comment"
    assert "2 test steps (4 jobs) instead of 2 (3 jobs)" in body
    assert "| NVIDIA, CPU and others | 2 (3) | 2 (4) | 1 (2) | 1 (3) |" in body
    assert "| AMD mirrors | 1 (1) | 1 (1) | 0 (0) | 0 (0) |" in body


def test_skips_and_adds_are_listed_with_who_added_them():
    body = render(_selection())
    skip = body.split("Would skip (today's rules run them) (1)")[1].split("</details>")[
        0
    ]
    add = body.split("Would add (today's rules do not run them) (1)")[1].split(
        "</details>"
    )[0]
    assert "`b` ×2" in skip
    assert "`d` ×3 (Python record)" in add


def test_an_amd_mirror_is_never_confused_with_its_parent():
    """The same name on both sides, one a mirror: skipping the mirror is not
    skipping the parent."""
    s = _selection(
        today=[StepView("a", 1), StepView("a", 1, "amd")],
        selector=[StepView("a", 1)],
        added_by={},
    )
    body = render(s)
    assert "| NVIDIA, CPU and others | 1 (1) | 1 (1) | 0 (0) | 0 (0) |" in body
    assert "| AMD mirrors | 1 (1) | 0 (0) | 1 (1) | 0 (0) |" in body


def test_run_all_says_so():
    body = render(_selection(run_all="a changed file runs everything"))
    assert "would run everything (a changed file runs everything)" in body


class FakeGh:
    def __init__(self, comments_pages, login="me"):
        self.pages = comments_pages
        self.login = login
        self.calls = []

    def __call__(self, *args, input_text=None):
        self.calls.append((args, input_text))
        if args[:2] == ("api", "user"):
            return self.login + "\n"
        if "--paginate" in args:
            # gh prints one JSON array per page, back to back
            return "".join(json.dumps(p) for p in self.pages)
        return json.dumps({"html_url": "https://example/c"})


def _write_call(gh):
    ((args, payload),) = [c for c in gh.calls if "-X" in c[0]]
    return args, json.loads(payload)


def test_post_edits_its_own_marked_comment():
    gh = FakeGh(
        [
            [{"id": 1, "user": {"login": "someone"}, "body": MARKER + " theirs"}],
            [{"id": 2, "user": {"login": "me"}, "body": MARKER + " mine"}],
        ]
    )
    assert post(5, "new body", gh=gh) == "https://example/c"
    args, payload = _write_call(gh)
    assert args[2:4] == ("PATCH", "repos/vllm-project/vllm/issues/comments/2")
    assert payload == {"body": "new body"}


def test_post_creates_when_it_has_no_comment_yet():
    """Another account's marked comment is never edited."""
    gh = FakeGh([[{"id": 1, "user": {"login": "someone"}, "body": MARKER}], []])
    post(5, "body", gh=gh)
    args, _ = _write_call(gh)
    assert args[2:4] == ("POST", "repos/vllm-project/vllm/issues/5/comments")

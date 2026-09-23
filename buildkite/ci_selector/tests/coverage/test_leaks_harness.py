# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""The leak replay's plumbing. The replay itself needs vLLM history and runs
by hand (`ci-validate leaks`)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from ci_selector.validate import leaks


def test_default_corpus_is_the_repository_one():
    assert leaks.DEFAULT_LEAKS.name == "selection-leaks.json"
    assert leaks.DEFAULT_LEAKS.parent.name == "test-selection"
    assert leaks.DEFAULT_LEAKS.is_file(), "the corpus moved; update DEFAULT_LEAKS"
    records = json.loads(leaks.DEFAULT_LEAKS.read_text())["records"]
    assert records and all(
        {"job_key", "culprit_pr", "pr_ci", "main_failure"} <= set(r) for r in records
    )


def test_empty_corpus_is_a_failure(tmp_path, capsys):
    corpus = tmp_path / "leaks.json"
    corpus.write_text(json.dumps({"records": []}))
    rc = leaks.run(
        SimpleNamespace(repo=tmp_path, leaks=corpus, json_out=None, table=None)
    )
    assert rc == 1 and "nothing to replay" in capsys.readouterr().out


def test_spellings_use_the_buildkite_key():
    step = SimpleNamespace(step_id="vllm_ci:foo:amd", buildkite_key="amd-foo")
    state = SimpleNamespace(pipelines=[SimpleNamespace(steps=[step])])
    assert leaks._spellings(state) == {"vllm_ci:foo:amd": "amd-foo"}

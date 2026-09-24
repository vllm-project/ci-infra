# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Publishing a coverage table and fetching it back.

Publisher and fetcher are written apart and have to agree on the prefix, the
file name and a pointer whose commit matches the table it names. These drive
the real publisher into a stubbed bucket and the real fetcher back out of it.
"""

from __future__ import annotations

import gzip
import json
import urllib.error
from pathlib import Path

import pytest
from ci_selector.coverage.source import TABLE_NAME
from ci_selector.coverage.table import load
from ci_selector.scripts import fetch_functions, publish
from ci_selector.scripts.build import merge_build, write_table

from .helpers import Build, process_file


@pytest.fixture
def table(tmp_path: Path, tmp_repo):
    build = Build(tmp_path / "sweep" / "b1", "42", tmp_repo.head())
    process_file(
        build.job("j0", step_key="runs-it") / "fn.a.txt", [("mod.py", "plain")]
    )
    out = tmp_path / TABLE_NAME
    write_table(merge_build(build.finish(), tmp_repo.root), out)
    return out


class TestSource:
    def test_a_built_table_records_where_it_came_from(self, table, tmp_repo):
        loaded = load(table)
        assert loaded.commit == tmp_repo.head()
        assert loaded.build == "42"
        assert loaded.source["builds"] == ["42"]

    def test_a_table_built_before_this_still_loads(self, table):
        """Nothing in the decision path reads `source`, so a table without it
        still has to load."""
        payload = json.loads(gzip.decompress(table.read_bytes()))
        del payload["source"]
        old = table.parent / "old.json.gz"
        old.write_bytes(gzip.compress(json.dumps(payload).encode()))
        loaded = load(old)
        assert loaded.available and len(loaded) == 1
        assert loaded.commit == ""


class TestPublish:
    def stub_aws(self, tmp_path, monkeypatch, fail_on=None):
        """An `aws s3 cp` that writes into a directory, and can refuse one."""
        bucket = tmp_path / "s3"
        calls = []

        real = publish.subprocess.run

        def run(cmd, **kwargs):
            # Only aws: git shells out through this same patched module.
            if cmd[0] != "aws":
                return real(cmd, **kwargs)
            src, dest = Path(cmd[3]), cmd[4]
            calls.append(dest)
            if fail_on and fail_on in dest:
                return type("P", (), {"returncode": 1, "stderr": "denied"})()
            target = bucket / dest.replace("s3://", "")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src.read_bytes())
            return type("P", (), {"returncode": 0, "stderr": ""})()

        monkeypatch.setattr(publish.subprocess, "run", run)
        return bucket, calls

    def test_publishes_under_the_commit_then_moves_the_pointer(
        self, table, tmp_path, monkeypatch, tmp_repo, capsys
    ):
        bucket, calls = self.stub_aws(tmp_path, monkeypatch)
        assert publish.main([str(table), "--bucket", "bkt"]) == 0
        commit = tmp_repo.head()
        assert calls == [
            f"s3://bkt/ci/fnrec/{commit}/{TABLE_NAME}",
            "s3://bkt/ci/fnrec/latest.json",
        ], "the pointer moves last, so a reader never finds a half-written table"
        pointer = json.loads((bucket / "bkt/ci/fnrec/latest.json").read_text())
        assert pointer["commit"] == commit
        assert pointer["files"] == [TABLE_NAME]

    def test_a_failed_table_upload_never_moves_the_pointer(
        self, table, tmp_path, monkeypatch
    ):
        bucket, calls = self.stub_aws(tmp_path, monkeypatch, fail_on=TABLE_NAME)
        assert publish.main([str(table), "--bucket", "bkt"]) == 1
        assert not (bucket / "bkt/ci/fnrec/latest.json").exists()
        assert len(calls) == 1, "it stopped rather than pointing at nothing"

    def test_dry_run_uploads_nothing(self, table, tmp_path, monkeypatch):
        bucket, calls = self.stub_aws(tmp_path, monkeypatch)
        assert publish.main([str(table), "--bucket", "bkt", "--dry-run"]) == 0
        assert calls == []

    def test_refuses_a_table_spanning_more_than_one_commit(
        self, table, tmp_path, monkeypatch, capsys
    ):
        """There is no single commit to publish it under."""
        payload = json.loads(gzip.decompress(table.read_bytes()))
        payload["source"]["commit"] = ""
        payload["source"]["commits"] = ["aaa", "bbb"]
        mixed = table.parent / "mixed.json.gz"
        mixed.write_bytes(gzip.compress(json.dumps(payload).encode()))
        self.stub_aws(tmp_path, monkeypatch)
        assert publish.main([str(mixed), "--bucket", "bkt"]) == 1
        assert "spans 2 commits" in capsys.readouterr().err

    def test_refuses_a_table_spanning_more_than_one_pipeline(
        self, table, tmp_path, monkeypatch, capsys
    ):
        """Blending two pipelines leaves no one prefix to publish under, and
        guessing would file it against a tree it does not describe."""
        payload = json.loads(gzip.decompress(table.read_bytes()))
        payload["source"]["pipeline"] = ""
        payload["source"]["pipelines"] = ["ci", "ci-torch-nightly"]
        blended = table.parent / "blended.json.gz"
        blended.write_bytes(gzip.compress(json.dumps(payload).encode()))
        self.stub_aws(tmp_path, monkeypatch)
        assert publish.main([str(blended), "--bucket", "bkt"]) == 1
        assert "spans 2 pipelines" in capsys.readouterr().err

    def test_refuses_an_unreadable_table(self, tmp_path, monkeypatch):
        bad = tmp_path / TABLE_NAME
        bad.write_bytes(b"not a table")
        self.stub_aws(tmp_path, monkeypatch)
        assert publish.main([str(bad), "--bucket", "bkt"]) == 1


class TestFetch:
    def serve(self, monkeypatch, files: dict[str, bytes]):
        def fake_get(url):
            if url not in files:
                raise urllib.error.HTTPError(url, 403, "denied", {}, None)
            return files[url]

        monkeypatch.setattr(fetch_functions, "_get", fake_get)

    def test_follows_the_pointer_and_validates(
        self, table, tmp_path, monkeypatch, tmp_repo
    ):
        base, commit = "https://b/ci/fnrec", tmp_repo.head()
        self.serve(
            monkeypatch,
            {
                f"{base}/latest.json": json.dumps(
                    {"commit": commit, "build": "42"}
                ).encode(),
                f"{base}/{commit}/{TABLE_NAME}": table.read_bytes(),
            },
        )
        out = tmp_path / "out"
        assert fetch_functions.main(["--url", base, "--out", str(out)]) == 0
        assert load(out / TABLE_NAME).commit == commit

    def test_reports_nothing_published(self, tmp_path, monkeypatch):
        self.serve(monkeypatch, {})
        out = tmp_path / "out"
        assert fetch_functions.main(["--url", "https://b/ci/fnrec", "--out", str(out)])
        assert not (out / TABLE_NAME).exists()

    def test_a_bad_download_leaves_the_previous_table_alone(
        self, table, tmp_path, monkeypatch
    ):
        base = "https://b/ci/fnrec"
        self.serve(
            monkeypatch,
            {
                f"{base}/latest.json": json.dumps({"commit": "c9"}).encode(),
                f"{base}/c9/{TABLE_NAME}": b"truncated",
            },
        )
        out = tmp_path / "out"
        out.mkdir()
        good = out / TABLE_NAME
        good.write_bytes(table.read_bytes())
        assert fetch_functions.main(["--url", base, "--out", str(out)]) == 1
        assert good.read_bytes() == table.read_bytes()

    def test_refuses_a_table_that_records_another_commit(
        self, table, tmp_path, monkeypatch, capsys
    ):
        """Pointer and table are separate uploads, so they can disagree."""
        base = "https://b/ci/fnrec"
        self.serve(
            monkeypatch,
            {
                f"{base}/latest.json": json.dumps({"commit": "deadbeef"}).encode(),
                f"{base}/deadbeef/{TABLE_NAME}": table.read_bytes(),
            },
        )
        out = tmp_path / "out"
        assert fetch_functions.main(["--url", base, "--out", str(out)]) == 1
        assert "refusing it" in capsys.readouterr().err
        assert not (out / TABLE_NAME).exists()

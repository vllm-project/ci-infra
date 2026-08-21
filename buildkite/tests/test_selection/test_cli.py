import sys

from test_selection import cli


def test_cli_converts_unexpected_selector_exception_to_fallback_exit(
    monkeypatch, capsys
):
    def broken_metadata(_graph):
        raise KeyError("repository_sha")

    monkeypatch.setattr(cli, "graph_metadata", broken_metadata)
    monkeypatch.setattr(
        sys,
        "argv",
        ["vllm-test-selection", "inspect-graph", "--graph", "missing.sqlite"],
    )

    assert cli.main() == 2
    assert "repository_sha" in capsys.readouterr().err


def test_cli_promote_snapshot_passes_only_pinned_inputs(monkeypatch, capsys, tmp_path):
    store = object()
    observed = {}

    monkeypatch.setattr(cli, "Boto3ObjectStore", lambda bucket: store)

    def promote(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return {"destination_prefix": "test-selection/vllm"}

    monkeypatch.setattr(cli, "promote_snapshot", promote)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vllm-test-selection",
            "promote-snapshot",
            "--bucket",
            "example-bucket",
            "--source-prefix",
            "test-selection/vllm/canary/verified",
            "--repo",
            str(tmp_path),
            "--repository-sha",
            "a" * 40,
            "--manifest-sha256",
            "b" * 64,
            "--graph-sha256",
            "c" * 64,
            "--max-snapshot-age-days",
            "5",
        ],
    )

    assert cli.main() == 0
    assert observed == {
        "args": (
            store,
            "test-selection/vllm/canary/verified",
            tmp_path,
            "a" * 40,
            "b" * 64,
            "c" * 64,
        ),
        "kwargs": {"max_age_days": 5},
    }
    assert '"destination_prefix": "test-selection/vllm"' in capsys.readouterr().out

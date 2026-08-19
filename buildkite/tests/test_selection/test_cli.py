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

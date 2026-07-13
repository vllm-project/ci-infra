import copy
from pathlib import Path

import pytest
import yaml

from pipeline_renderer import render_pipeline_document, render_pipeline_yaml

FIXTURES = Path(__file__).resolve().parent / "test_files"


def test_render_pipeline_yaml_matches_native_pipeline_golden_file():
    pipeline_yaml = (FIXTURES / "native_pipeline_input.yaml").read_text()
    expected = yaml.safe_load((FIXTURES / "native_pipeline_expected.yaml").read_text())

    rendered = yaml.safe_load(
        render_pipeline_yaml(pipeline_yaml, ["src/gpu/kernel.py"])
    )

    assert rendered == expected


def test_render_pipeline_document_does_not_mutate_input():
    document = {
        "steps": [
            {
                "label": "unit tests",
                "command": "pytest",
                "source_file_dependencies": ["src/"],
            }
        ]
    }
    original = copy.deepcopy(document)

    render_pipeline_document(document, ["src/runtime.py"])

    assert document == original


def test_unknown_changes_keep_every_step_and_strip_metadata_recursively():
    document = {
        "source_file_dependencies": ["pipeline/"],
        "steps": [
            {
                "group": "tests",
                "source_file_dependencies": ["src/"],
                "steps": [
                    {
                        "label": "unit tests",
                        "source_file_dependencies": ["tests/"],
                    }
                ],
            }
        ],
    }

    rendered = render_pipeline_document(document, None)

    assert rendered == {
        "steps": [{"group": "tests", "steps": [{"label": "unit tests"}]}]
    }


def test_empty_change_list_only_keeps_unconditional_steps():
    document = {
        "steps": [
            {
                "label": "conditional",
                "source_file_dependencies": ["src/"],
            },
            {"wait": None},
            {"label": "always"},
        ]
    }

    rendered = render_pipeline_document(document, [])

    assert rendered == {"steps": [{"wait": None}, {"label": "always"}]}


def test_nonmatching_group_dependency_removes_the_whole_group():
    document = {
        "steps": [
            {
                "group": "GPU tests",
                "source_file_dependencies": ["src/gpu/"],
                "steps": [{"label": "test"}],
            }
        ]
    }

    rendered = render_pipeline_document(document, ["docs/index.md"])

    assert rendered == {"steps": []}


def test_group_is_removed_when_all_of_its_children_are_filtered_out():
    document = {
        "steps": [
            {
                "group": "GPU tests",
                "steps": [
                    {
                        "label": "GPU unit tests",
                        "source_file_dependencies": ["src/gpu/"],
                    }
                ],
            }
        ]
    }

    rendered = render_pipeline_document(document, ["docs/index.md"])

    assert rendered == {"steps": []}


def test_non_mapping_step_shorthand_is_preserved():
    document = {"steps": ["wait"]}

    rendered = render_pipeline_document(document, [])

    assert rendered == document


@pytest.mark.parametrize(
    "dependencies",
    ["src/", ["src/", 3], {"path": "src/"}],
)
def test_invalid_dependency_metadata_raises(dependencies):
    document = {
        "steps": [
            {
                "label": "invalid",
                "source_file_dependencies": dependencies,
            }
        ]
    }

    with pytest.raises(ValueError, match="source_file_dependencies"):
        render_pipeline_document(document, None)


@pytest.mark.parametrize("dependency", ["src/gpu", "src/gpu/"])
def test_dependencies_match_exact_paths_and_directory_prefixes(dependency):
    document = {
        "steps": [
            {
                "label": "matching",
                "source_file_dependencies": [dependency],
            }
        ]
    }

    exact = render_pipeline_document(document, ["src/gpu"])
    nested = render_pipeline_document(document, ["src/gpu/kernel.py"])
    substring = render_pipeline_document(document, ["src/gpu_extra.py"])

    assert exact == {"steps": [{"label": "matching"}]}
    assert nested == {"steps": [{"label": "matching"}]}
    assert substring == {"steps": []}

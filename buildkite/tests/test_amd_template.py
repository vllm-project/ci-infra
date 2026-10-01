import re
from pathlib import Path

import minijinja
import pytest
import yaml


@pytest.mark.parametrize("dind", [False, True])
@pytest.mark.parametrize(
    "label,slug",
    [
        (
            ":amd: (MI355) LM Eval DeepSeek V3.2 TP",
            ":amd:-mi355-lm-eval-deepseek-v3-2-tp",
        ),
        (
            ":amd: (MI355) LM Eval DeepSeek V3.2 DP",
            ":amd:-mi355-lm-eval-deepseek-v3-2-dp",
        ),
        (":amd: (MI355) Prefill/Decode", ":amd:-mi355-prefill-decode"),
        ("Tokens@128", "tokens-128"),
        ("Model café", "model-caf-"),
        (
            ":amd: (MI355) Quant, FP8+FP4 Shard %N",
            ":amd:-mi355-quant--fp8-fp4-shard-n",
        ),
        (":amd: (MI355) test_name: FP8-FP4", ":amd:-mi355-test_name:-fp8-fp4"),
    ],
)
def test_amd_template_block_keys_and_dependencies(label, slug, dind):
    template = (Path(__file__).parents[1] / "test-template-amd.j2").read_text()
    rendered = minijinja.render_str(
        template,
        branch="test-branch",
        list_file_diff="",
        mirror_hw="amdproduction",
        run_all="1",
        nightly="0",
        steps=[
            {
                "label": label,
                "agent_pool": "mi355_4",
                "mirror_hardwares": ["amdproduction"],
                "optional": True,
                "dind": dind,
                "commands": ["true"],
            }
        ],
    )
    block, command = yaml.safe_load(rendered)[0]["steps"][-2:]
    expected_key = f"block-mi355_4-{slug}"
    assert re.fullmatch(r"[A-Za-z0-9_:-]+", block["key"])
    assert block["key"] == expected_key
    assert block["block"] == f"Run mi355_4: {label}"
    assert command["label"] == f"mi355_4: {label}"
    assert command["depends_on"] == (
        expected_key if dind else [expected_key, "verify-native-ci-base-amd"]
    )

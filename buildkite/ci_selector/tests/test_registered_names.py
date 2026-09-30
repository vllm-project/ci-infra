# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Registered-key routing: true-positive pins (the no-loss proof for typed
matching) + matcher units."""

import pytest
import regex as re
from helpers import always_run_ids, drift_message, steps_by_id


def _steps_for(state, key):
    return {sid for sid, ks in state.keys.step_keys.items() if key in ks}


def _flag_key_contexts(state, flag: str) -> dict[str, set[str]]:
    """step_id -> the registered parser names `flag` passes in that step.

    The premise side of the typed matcher, read off the same text it reads.
    Every live context arrives through a step's config data_files, so the
    command-haystack arm has no end-to-end pin -- recorded, not asserted,
    since asserting it would fail today.

    A copy of the matcher's pattern rather than a call to it, so the premise
    and the assertion are not the same code. It fails in one direction only:
    if the matcher stops keying a context this still finds, the test goes red,
    but a context shape this pattern misses never enters the set.
    """
    pattern = re.compile(re.escape(flag) + r"[\"']?[ =:]+[\"']?([\w.-]+)")
    parsers = set(state.full.factories.parser_entries)
    found: dict[str, set[str]] = {}
    for sid, text in state.keys.searchable.items():
        named = set(pattern.findall(text)) & parsers
        if named:
            found[sid] = named
    return found


def test_register_key_value_contexts_pinned(state):
    """PR #50326 anchor: NixlConnector selects the PD/accuracy e2e fleet via
    quoted/assigned config values; must survive any matcher change."""
    nixl = _steps_for(state, "NixlConnector")
    assert len(nixl) >= 15, sorted(nixl)
    assert any("nixlconnector-pd-accuracy" in s for s in nixl)


def test_parser_key_flag_contexts_pinned(state):
    """Premise on the raw searchable text (a flag context still exists),
    assertion on the matcher output.

    Existential over every live flag, naming no specimen. The jobs that used
    to be named only ever covered the flag, which this covers whole.
    """
    from ci_selector.codemap.registered_names import PARSER_SELECTING_FLAGS

    live = {
        flag: found
        for flag in PARSER_SELECTING_FLAGS
        if (found := _flag_key_contexts(state, flag))
    }
    # Two, not all of them: the snake_case spellings match config and env
    # contexts, and no step passes a parser that way.
    assert len(live) >= 2, drift_message(
        f"only {len(live)} of {len(PARSER_SELECTING_FLAGS)} parser-selecting "
        "flags still pass a registered parser anywhere in CI",
        "a flag context is how a job command routes to the parser file it "
        "names; below two live flags this stops proving the typed matcher "
        "extracts anything at all, and the jobs quietly fall back to broader "
        "matching",
        "if vLLM renamed the flags, PARSER_SELECTING_FLAGS in "
        "codemap/registered_names.py is what to update",
        "if the eval jobs stopped passing a parser by flag, the typed parser "
        "arm has no live user and the mechanism is what to question",
    )
    pairs = {
        (sid, key)
        for found in live.values()
        for sid, named in found.items()
        for key in named
    }
    # Mirrors re-run their parent's command text, so they are the same context
    # counted twice; the jobs are the evidence.
    by_id = steps_by_id(state)
    jobs = {sid for sid, _ in pairs if not by_id[sid].mirror_hw}
    assert len(jobs) >= 2, drift_message(
        f"the {len(pairs)} live flag contexts come from {len(jobs)} distinct "
        f"job(s): {sorted(jobs)}",
        "the flag count cannot see this: one job passing three flags keeps "
        "three of them live by itself, so the existential can come to rest on "
        "a single eval config without the floor moving -- and then one "
        "relabel or one dropped argument takes the typed arm's whole "
        "end-to-end evidence with it",
        "if the eval fleet really is down to one job, this stops being an "
        "existential over the flag dimension: pin that job by name and say "
        "what it proves, or retire the case",
        "if the eval jobs stopped passing a parser by flag, the typed parser "
        "arm has no live user and the mechanism is what to question",
    )
    for flag, found in sorted(live.items()):
        for sid, named in sorted(found.items()):
            missing = named - state.keys.step_keys.get(sid, set())
            assert not missing, f"{sid} passes {flag} {sorted(missing)}, unkeyed"


@pytest.mark.drift
def test_parser_flag_re_flags_exist_in_vllm(vllm_repo):
    """Rot guard: every flag in _PARSER_FLAG_RE must still exist in vLLM.
    A phantom flag (the never-existent --renderer) silently degrades typed matching."""
    import subprocess

    from ci_selector.codemap.registered_names import _PARSER_FLAG_RE

    cost = (
        "These flags are how a job command like `--kv-connector NixlConnector` "
        "routes to the code that registers that name. A flag we watch for that "
        "vLLM does not have matches nothing, so those jobs lose their typed "
        "edge and fall back to broader matching."
    )
    body = _PARSER_FLAG_RE.removeprefix("(?:").removesuffix(")")
    # Every alternative, not just the `--` spellings. The snake_case forms are
    # what match config and env contexts, and filtering them out left half the
    # pattern unchecked: a renamed config field moved nothing.
    flags = [a for a in body.split("|") if a]
    assert len(flags) >= 4, drift_message(
        f"Only {len(flags)} alternatives could be read out of _PARSER_FLAG_RE, "
        "so this guard is checking almost nothing.",
        cost,
        "the pattern changed shape: update the split in this test to match "
        "_PARSER_FLAG_RE in ci_selector/codemap/registered_names.py",
    )
    missing = [
        flag
        for flag in flags
        if subprocess.run(
            ["grep", "-rq", "--", flag, str(vllm_repo / "vllm")]
        ).returncode
        != 0
    ]
    assert not missing, drift_message(
        f"_PARSER_FLAG_RE watches flags that no longer appear in vLLM: {missing}",
        cost,
        "the flag was renamed upstream: update _PARSER_FLAG_RE in "
        "ci_selector/codemap/registered_names.py",
        "the flag was removed: drop it from _PARSER_FLAG_RE",
    )


def test_hf_id_env_pin(state):
    assert (
        "deepseek-ai/DeepSeek-V2-Lite-Chat"
        in state.keys.step_keys["vllm_ci:2-node-test-4-gpus"]
    )


def test_register_value_context_matcher():
    from ci_selector.codemap.registered_names import _typed_pattern

    pat = _typed_pattern("NixlConnector", "register")
    assert pat.search('"kv_connector":"NixlConnector"')
    assert pat.search("KV_CONNECTOR=${KV_CONNECTOR:-NixlConnector}")
    assert pat.search('\\"NixlConnector\\"')
    assert pat.search("kv_connector=NixlConnector")
    ipc = _typed_pattern("ipc", "register")
    assert not ipc.search("docker run --ipc=host img")
    assert not ipc.search("some words about ipc handles")


def test_parser_flag_context_matcher():
    from ci_selector.codemap.registered_names import _typed_pattern

    pat = _typed_pattern("granite", "parser")
    assert pat.search("--tool-call-parser granite -x")
    assert pat.search('"tool_call_parser": "granite"')
    assert not pat.search("MODEL_NAMES=ibm-granite/granite-4.0-h-tiny")
    assert not pat.search("run tests/tool_use with granite fixtures")


def test_comment_lines_do_not_route_keys():
    from ci_selector.codemap.registered_names import _strip_comment_lines

    text = "# Default: TRITON_ATTN on ROCm\npytest v1/attention"
    assert "TRITON_ATTN" not in _strip_comment_lines(text)
    assert "pytest v1/attention" in _strip_comment_lines(text)


def test_scalar_literals_never_become_keys(state):
    """`1` and `true` come from an env truthiness test and match `-tp=1`,
    `sleep 1`, `|| true` all over the pipeline. Rejecting them by type is what
    replaced a step-fanout bar that could not tell them from a popular genuine
    key, and severed the genuine one."""
    from ci_selector.codemap.registered_names import _is_scalar_literal

    assert all(_is_scalar_literal(k) for k in ("1", "true", "0", "null"))
    assert not any(_is_scalar_literal(k) for k in ("mtp", "pooling", "fp8", "auto"))
    assert {"1", "true"} <= set(state.keys.refused)
    assert not any(_is_scalar_literal(k) for k in state.keys.key_mechanism)


def test_wide_dispatch_key_survives(state):
    """No key is dropped for naming too many steps. `mtp` is the specimen: it
    routes the spec-decode PD-accuracy and lm-eval steps, which reach it by
    config string and not by import, so losing it is silent under-selection."""
    from collections import Counter

    counts = Counter(key for ks in state.keys.step_keys.values() for key in ks)
    assert counts["mtp"] > 20, counts["mtp"]
    assert state.keys.key_mechanism.get("mtp") == "dispatch"
    routed = state.keys.steps_naming({"mtp"})
    assert any("pd-accuracy" in s for s in routed), sorted(routed)[:5]
    assert any("lm-eval" in s for s in routed), sorted(routed)[:5]


def test_dispatch_key_matches_only_as_a_value():
    """vllm#58975: fope.py minted the dispatch key 'default' from
    `rope_type == "default"`, and the bare word routed rust-frontend-cargo-*
    through rustup's --default-toolchain and ray-dependency-compatibility-check
    through echo prose. A gating literal is a config value, so it selects its
    member only where it stands as one. Every shape here is one CI uses, one
    case per branch of the pattern."""
    from ci_selector.codemap.registered_names import _typed_pattern

    values = {
        "mtp": [
            '--speculative-config \'{"method":"mtp","num_speculative_tokens":1}\'',
            "VLLM_SERVE_EXTRA_ARGS=--spec-method,mtp,--spec-tokens,1",
            'mtp_config="SD_METHOD=mtp MODEL_NAME=Qwen/Qwen3.5-0.8B"',
            "  --max-num-seqs 256\n  --spec-method mtp\n",
            "method: mtp",
            "SD_METHOD=${SD_METHOD:-mtp}",
            # a step env entry, rendered KEY=value into the haystack
            "VLLM_SERVE_EXTRA_ARGS=--spec-method mtp",
            "export VLLM_SERVE_EXTRA_ARGS='--trust-remote-code,--spec-method mtp'",
            "SERVE_ARGS=(--speculative-method mtp --num-speculative-tokens 1)",
            "speculative_methods:\n  - mtp\n",
        ],
        "dspark": [
            "  --speculative-config.method dspark\n",
            # vLLM infers the method from a draft model's name
            '"model": "RedHatAI/Kimi-K3-speculator.dspark",',
        ],
        "eagle3": [
            '--speculative-config \'{"model": '
            '"RedHatAI/Qwen3-8B-speculator.eagle3", "num_speculative_tokens": 3}\'',
        ],
        "eagle": [
            "spec_decode_offline.py --test --method eagle --num_spec_tokens 3",
            '\'{"model":"abhigoyal/vllm-eagle-llama-68m-random"}\'',
        ],
        "moonep": ["--enable-expert-parallel --all2all-backend=moonep"],
        "deepep_low_latency": [
            'BACKENDS=("deepep_high_throughput" "deepep_low_latency")',
            '--all2all-backends "deepep_low_latency,naive"',
        ],
        # Llama-3.2 is a llama3-rope model; a version suffix is the same name
        "llama3": [
            "pytest tool_use --models llama3.2 -k 'not x'",
            "test_chat_completion_with_tools[llama3.2]",
        ],
    }
    not_values = {
        "default": [
            "curl https://sh.rustup.rs | sh -s -- -y --default-toolchain none",
            'echo ">>>          Falling back to default PyPI (resolution may differ)"',
            'echo ">>> Using PyTorch index: ${TORCH_INDEX_URL:-PyPI default}"',
            'label="default backend"',
            'run_tests "default backend" ""',
            'local default="docker/Dockerfile.rocm_base"',
            '  echo "Commands sourced from default YAML: ${DEFAULT_YAML}"',
            "--default-chat-template-kwargs '{\"enable_thinking\": false}'",
        ],
        "linear": ["--linear-backend humming", "w8a8-fp8-linear)"],
        "suffix": ['local suffix=""', "--serialized-directory /tmp/ --suffix v1"],
        "mtp": ["pytest -v -s v1/e2e/spec_decode/mtp/", "--config mtp.yaml"],
    }
    for key, texts in values.items():
        pat = _typed_pattern(key, "dispatch")
        for text in texts:
            assert pat.search(text), (key, text)
    for key, texts in not_values.items():
        pat = _typed_pattern(key, "dispatch")
        for text in texts:
            assert not pat.search(text), (key, text)


def test_list_made_only_of_dispatch_keys_routes_each():
    """A space list reads like prose to the value pattern, which must not take
    'label="default backend"'. A list made only of dispatch keys is values:
    every backend of an all2all matrix, every method of a sweep."""
    from ci_selector.codemap.registered_names import _listed_keys

    keys = {"deepep_low_latency", "deepep_high_throughput", "mtp", "eagle3"}
    keys |= {"ngram", "default", "linear"}
    listed = {
        'ALL2ALL_BACKENDS="deepep_low_latency deepep_high_throughput" bash x.sh': {
            "deepep_low_latency",
            "deepep_high_throughput",
        },
        "BACKENDS=(deepep_low_latency deepep_high_throughput)": {
            "deepep_low_latency",
            "deepep_high_throughput",
        },
        'for m in eagle3 mtp ngram; do\n  bash run.sh --spec-method "$m"\ndone': {
            "eagle3",
            "mtp",
            "ngram",
        },
        '\\"mtp,eagle3\\"': {"mtp", "eagle3"},
    }
    for text, want in listed.items():
        assert _listed_keys(text, keys) == want, text
    for text in (
        'label="default backend"',
        'echo "running with default linear scaling"',
        "for f in *.yaml; do",
        'pytest -k "eagle3 and not mtp"',
    ):
        assert not _listed_keys(text, keys), text


def test_value_contexts_key_a_built_step(state):
    """Both value readers feed step_keys through KeyIndex.build: a step that
    only sweeps methods in a list, or only names a speculators draft model
    (vLLM infers eagle3 from it), is keyed; the vllm#58975 prose is not. No
    step at pinning time depends on either shape alone, so without this a
    build that dropped one would pass every other test."""
    from ci_selector.codemap.pipeline.targets import StepTargets
    from ci_selector.codemap.registered_names import KeyIndex
    from ci_selector.codemap.state import PipelineData

    for key in ("mtp", "eagle3", "ngram", "default"):
        assert state.keys.key_mechanism.get(key) == "dispatch", (
            f"specimen moved: {key!r} is no longer a dispatch key"
        )
    texts = {
        "sweep": 'SD_METHODS="eagle3 mtp ngram" bash sweep.sh',
        "draft": "vllm serve Qwen/Qwen3-8B --speculative-config "
        '\'{"model": "RedHatAI/Qwen3-8B-speculator.eagle3"}\'',
        "prose": 'echo "Falling back to default PyPI"\nlabel="default backend"',
    }
    pdata = PipelineData(
        config=state.pipelines[0].config,
        steps=[],
        targets={sid: StepTargets(sid, haystack=t) for sid, t in texts.items()},
    )
    keys = KeyIndex.build(state.repo, state.full, [pdata]).step_keys
    assert {"eagle3", "mtp", "ngram"} <= keys.get("sweep", set()), keys
    assert "eagle3" in keys.get("draft", set()), keys
    assert "default" not in keys.get("prose", set()), keys


def test_generic_dispatch_word_routes_no_step_by_prose(state):
    """vllm#58975 end to end. None of these steps has a recording row, so a
    key route to them is a step nothing narrows afterwards: every change
    reaching fope.py ran rust lint and a ray resolver check."""
    assert state.keys.key_mechanism.get("default") == "dispatch", (
        "specimen moved: 'default' is no longer a dispatch key; pick another "
        "gating literal that is an ordinary word in step commands"
    )
    named = state.keys.steps_naming({"default"})
    word = re.compile(r"\bdefault\b")
    for sid in (
        "vllm_ci:rust-frontend-cargo-style-clippy",
        "vllm_ci:rust-frontend-cargo-tests",
        "vllm_ci:ray-dependency-compatibility-check",
    ):
        assert word.search(state.keys.commands.get(sid, "")), (
            f"specimen moved: {sid} no longer says 'default'"
        )
        assert sid not in named, sid


def test_dispatch_key_config_contexts_pinned(state):
    """The no-loss side of value-only matching: a step that sets a method or
    backend to a dispatch key ('"method":"mtp"', '--spec-method,mtp',
    '--speculative-config.method dspark', '--all2all-backend=moonep') is keyed
    by it.

    A copy of the shape rather than a call to the matcher, so premise and
    assertion are not the same code."""
    dispatch = {k for k, m in state.keys.key_mechanism.items() if m == "dispatch"}
    setting = re.compile(
        r"(?i:method|backend)[\"']?(?:[ \t]*[:=,][ \t]*|[ \t]+)[\"']?([\w-]+)"
    )
    found: dict[str, set[str]] = {}
    for sid, text in state.keys.commands.items():
        named = set(setting.findall(text)) & dispatch
        if named:
            found[sid] = named
    keys = {k for ks in found.values() for k in ks}
    # mtp, eagle, eagle3 and dspark at pinning time, in 35 steps
    assert len(keys) >= 3, drift_message(
        f"only {sorted(keys)} are set as a method or backend in step commands",
        "this is the evidence that value-only matching still routes the eval "
        "and PD-accuracy jobs that pick a speculator or all2all backend by "
        "name; with fewer contexts it proves little",
        "if the jobs moved these settings elsewhere, widen `setting` here",
    )
    for sid, named in sorted(found.items()):
        missing = named - state.keys.step_keys.get(sid, set())
        assert not missing, f"{sid} sets {sorted(missing)}, unkeyed"


def test_value_matching_drops_only_word_routes(state):
    """The no-loss side over every dispatch key and every context: steps whose
    commands have the key as a word but are not keyed by it. At pinning time
    those keys are ordinary words (default, linear, suffix, dynamic) and one
    config-file-name fragment (cutedsl in ...-fi-cutedsl-deepep-ll.yaml).
    Any other key here is a value context the pattern stopped reading, such
    as a pytest node id (`tools[llama3.2]`) or a draft model's name.

    `\\bkey\\b` is the matcher dispatch keys had before, kept as a copy so
    the premise is not the code under test."""
    dispatch = {k for k, m in state.keys.key_mechanism.items() if m == "dispatch"}
    dropped: dict[str, set[str]] = {}
    for key in dispatch:
        word = re.compile(rf"\b{re.escape(key)}\b")
        for sid, text in state.keys.commands.items():
            if word.search(text) and key not in state.keys.step_keys.get(sid, ()):
                dropped.setdefault(key, set()).add(sid)
    known = {"default", "linear", "suffix", "dynamic", "cutedsl"}
    new = {k: sorted(v)[:3] for k, v in dropped.items() if k not in known}
    assert not new, drift_message(
        f"dispatch keys stop routing steps that name them as a word: {new}",
        "if the step sets the key as a value, the member's change no longer "
        "selects it, and nothing else does: demotion cut the import edge",
        "if the context is a value, teach _value_pattern or _listed_keys its "
        "shape and add a case to test_dispatch_key_matches_only_as_a_value",
        "if it is prose or part of a longer name, add the key to `known` here",
    )


def test_shared_gating_literal_keys_every_member(state):
    """Dispatch keys are typed now, and the mint refuses a literal a typed
    registration already owns. A literal gating a second member is shared,
    not owned: 'mtp' gates both eagle.py and gemma4.py."""
    assert not any(
        why == "typed-owned (dispatch)" for why in state.keys.refused.values()
    ), state.keys.refused
    for member in ("vllm/v1/spec_decode/eagle.py", "vllm/v1/spec_decode/gemma4.py"):
        assert "mtp" in state.keys.for_file(member), (
            f"specimen moved: mtp no longer gates {member}"
        )


def test_dropped_edges_separates_refused_from_unrouted():
    """The old predicate asked whether a literal existed anywhere in the index,
    so one owned by another file counted as routing this member, and a deleted
    one just slid the member into the skipped bucket."""
    from types import SimpleNamespace

    from ci_selector.codemap.registered_names import KeyIndex
    from helpers import key_selection_gaps

    keys = KeyIndex()
    keys.keyed_modules["vllm/a.py"] = {"alpha"}
    keys.refused["beta"] = "non-string scalar"
    state = SimpleNamespace(
        full=SimpleNamespace(
            dispatch=SimpleNamespace(
                demotions={("vllm/imp.py", "vllm/a.py"): {"alpha", "beta", "gamma"}},
                claims={"vllm/a.py"},
            )
        ),
        keys=keys,
        auto_step_ids=set(),
    )
    _gaps, unrouted, checked = key_selection_gaps(state)
    assert unrouted == [("vllm/a.py", "gamma")]
    assert checked == 0


def test_fanout_bar_loss_needs_a_registered_key_with_no_route():
    """The bar drops registered keys as well as English words ("pooling" is one
    at HEAD), so a drop is only a loss when the key index says the literal
    routes somewhere and nothing survives to reach it."""
    from types import SimpleNamespace

    from ci_selector.codemap.graph.demote import CONFIG_KEY_MAX_TEST_FILES as BAR
    from ci_selector.codemap.graph.imports import ImportGraph
    from ci_selector.codemap.registered_names import KeyIndex
    from helpers import fanout_dropped_literals

    graph = ImportGraph()
    for lit in ("english", "keyed", "routed"):
        for n in range(BAR + 1):  # every literal is over the bar
            graph.string_literals[f"tests/{lit}/test_{n}.py"] = {lit}

    keys = KeyIndex()
    keys.key_mechanism["keyed"] = "dispatch"  # registered, routes no auto step
    keys.key_mechanism["routed"] = "dispatch"
    keys.step_keys = {"vllm_ci:s": {"routed"}}
    state = SimpleNamespace(
        full=SimpleNamespace(
            graph=graph,
            dispatch=SimpleNamespace(
                demotions={
                    ("vllm/imp.py", "vllm/m.py"): {"english", "keyed", "routed"}
                },
            ),
        ),
        keys=keys,
        auto_step_ids={"vllm_ci:s"},
    )
    losses, dropped = fanout_dropped_literals(state)
    assert dropped == 3
    assert losses == [("vllm/m.py", "keyed")]


def test_key_routing_is_belt_over_graph_coverage(state):
    """Fallback: with key routing disabled entirely, a parser file still
    selects the steps covering its own test files via the import graph."""
    import dataclasses

    from ci_selector.codemap.classify import select
    from ci_selector.codemap.registered_names import KeyIndex

    bare_keys = KeyIndex(searchable=dict(state.keys.searchable))
    bare = dataclasses.replace(state, keys=bare_keys)
    sel = select(bare, ["vllm/tool_parsers/granite_tool_parser.py"])
    assert not sel.run_all
    always = always_run_ids(state)
    assert set(sel.selected) - always, "graph channel must still select"


def test_short_key_word_boundary_units():
    import regex as re
    from ci_selector.codemap.registered_names import match_keys

    patterns = {
        "inc": re.compile(r"\binc\b"),
        "fp8": re.compile(r"\bfp8\b"),
    }
    hits = match_keys(set(), patterns, {}, set(), "use --include=x and fp8 quant")
    assert hits == {"fp8"}, "inc must not match inside include"
    assert "inc" in match_keys(set(), patterns, {}, set(), "backend inc selected")
    assert not match_keys(set(), patterns, {}, set(), "xfp8 only"), (
        "fp8 must not match inside a word"
    )


def test_substring_key_with_metachars_safe():
    from ci_selector.codemap.registered_names import match_keys

    hits = match_keys({"org/model+x"}, {}, {}, set(), "eval run org/model+x here")
    assert hits == {"org/model+x"}


def test_target_literal_routes_without_haystack_hit():
    """Key absent from the command haystack still routes on the step's own
    target test file carrying it as a string literal."""
    from ci_selector.codemap.registered_names import match_keys

    hits = match_keys({"org/model+x"}, {}, {}, {"org/model+x"}, "unrelated cmd")
    assert hits == {"org/model+x"}


def test_matching_thresholds_pinned():
    from ci_selector.codemap.registered_names import (
        RAW_KEY_MIN_LEN,
        SUBSTRING_KEY_MIN_LEN,
    )

    assert SUBSTRING_KEY_MIN_LEN == 12
    # 18 real archs are 8-11 chars slash-free; raising the raw bar drops
    # them from table-diff head-side routing (under-selection direction).
    assert RAW_KEY_MIN_LEN == 8


def test_parser_key_env_default_context():
    from ci_selector.codemap.registered_names import _typed_pattern

    pat = _typed_pattern("openai", "parser")
    assert pat.search('TOOL_CALL_PARSER="${BFCL_TOOL_CALL_PARSER:-openai}"')
    assert not pat.search("pytest entrypoints/openai -v")


def test_colliding_parser_key_keeps_all_module_files(state):
    """kimi_k3 registers as a tool parser AND a tokenizer mode; the merged
    parser_entries dict is last-wins (tokenizers parse after tool_parsers),
    which pointed the key at vllm/tokenizers/hf.py alone and silently cut the
    parser file's key route. Every registering file must keep the key."""
    parser_file = "vllm/tool_parsers/kimi_k3_tool_parser.py"
    assert "kimi_k3" in state.keys.for_file(parser_file), (
        "kimi_k3 no longer keys its parser file; if the collision is gone "
        "pick a new colliding specimen before deleting this test"
    )
    assert "kimi_k3" in state.keys.for_file("vllm/tokenizers/hf.py")


def test_directory_targets_fold_test_literals(state):
    """A step whose pytest target is a DIRECTORY carries the keys its test
    files pin. entrypoints-integration-api-server-generate runs
    `pytest tool_use` and declares only bare vllm/ (the B4 shape); without
    the fold its step_keys were empty and every parser key lost the belt
    route that survives an import edge going missing."""
    sid = "vllm_ci:entrypoints-integration-api-server-generate"
    lits = state.full.graph.string_literals
    assert "kimi_k3" in lits.get("tests/tool_use/test_kimi_k3_tool_parser.py", set()), (
        "specimen moved: pick another parser key pinned by a test under a "
        "directory target"
    )
    assert "kimi_k3" in state.keys.step_keys.get(sid, set())


def test_directory_fold_excludes_non_test_files(state):
    """tests/tool_use/utils.py pins the whole parser matrix; helper literals
    must not join the fold. A helper names parsers the step may never run,
    and an imported helper is already the graph's job to cover."""
    sid = "vllm_ci:entrypoints-integration-api-server-generate"
    assert "internlm" in state.full.graph.string_literals.get(
        "tests/tool_use/utils.py", set()
    ), "specimen moved: utils.py no longer pins internlm"
    assert "internlm" not in state.keys.step_keys.get(sid, set())


def test_tool_parser_key_routing_floor(state):
    """Detection floor for the whole parser-key channel: a moved registry
    table or a broken fold must read as loud failure, not as quietly-empty
    routing. 47 tool-parser entries and 44/57 merged keys routed at pinning
    time; the bars sit far below that so ordinary churn passes."""
    from ci_selector.codemap.graph.factories import TOOL_PARSER_INIT

    counts = state.full.factories.parser_table_counts
    assert counts.get(TOOL_PARSER_INIT, 0) > 40, counts
    entries = state.full.factories.parser_entries
    routed = {key for key in entries if state.keys.steps_naming({key})}
    assert len(routed) >= 30, (
        f"only {len(routed)}/{len(entries)} parser keys reach any step; "
        "the key-routing belt has collapsed"
    )


# A flag whose name ends in `parser`, and the trailing character that proves it
# ended there. --tool-parser-plugin names a plugin module, not a parser.
PARSER_FLAG_SHAPE = r"--[a-z0-9-]*parser([^a-z0-9-]|$)"


@pytest.mark.drift
def test_no_parser_selecting_flag_is_unwatched(vllm_repo):
    """The other direction, which the rot guard above cannot see.

    A flag we watch that vLLM dropped matches nothing and is caught above. A
    flag vLLM added that we do not watch is the expensive one: those jobs never
    get a typed edge to the parser they name, so a change to that parser stops
    selecting them and nothing anywhere goes red.
    """
    import subprocess

    import regex as re
    from ci_selector.codemap.registered_names import PARSER_SELECTING_FLAGS

    # The whole tree, not just cli_args.py: only --tool-call-parser is spelled
    # there, so scanning that one file left the other flags unwatched by this.
    # `parser` must end the flag, since --tool-parser-plugin names a plugin
    # module rather than selecting a parser.
    hits = subprocess.run(
        # -e, or grep reads the pattern's leading -- as an unknown option and
        # exits 2 with no output. The floor below is what would actually catch
        # that; -e keeps the failure from being a confusing one.
        ["grep", "-rhoE", "-e", PARSER_FLAG_SHAPE, str(vllm_repo / "vllm")],
        capture_output=True,
        text=True,
    ).stdout
    found = {re.sub(r"[^a-z-]+$", "", line) for line in hits.split() if line}
    assert len(found) >= 2, drift_message(
        f"Only {len(found)} parser-selecting flags were found in vllm/: {found}.",
        "This guard reads the tree to notice a flag we do not watch. Finding "
        "none finds no gaps, which looks exactly like watching all of them.",
        "the flags moved or changed shape: update the pattern in this test",
    )
    unwatched = sorted(found - set(PARSER_SELECTING_FLAGS))
    assert not unwatched, drift_message(
        f"vLLM has parser-selecting flags we do not watch: {unwatched}",
        "Typed matching is how `--reasoning-parser qwen3` in a job command "
        "routes that job to the parser it names. An unwatched flag leaves "
        "those jobs unrouted, so editing the parser stops selecting them.",
        "add the flag, and its snake_case config spelling, to "
        "PARSER_SELECTING_FLAGS in ci_selector/codemap/registered_names.py",
    )

#!/usr/bin/env python3
"""Build a smoke-test plan from recipes.vllm.ai.

usage: make_plan.py --hw {b200,h200} --lanes {1,2} --out plan.json MODEL[:VARIANT] ...
  e.g. make_plan.py --hw b200 --lanes 2 --out plan_b200.json \
         deepseek-ai/DeepSeek-V4.1-Flash zai-org/GLM-5.3:nvfp4 Qwen/Qwen3.5-397B-A17B:nvfp4

Per model: base_args + variant/hardware extra_args + non-opt-in feature args
(tool calling, reasoning) + recommended_command env. If the recipe's
recommended_command targets the same GPU family, its argv is used instead.
TP = smallest power of two whose total VRAM covers vram_minimum_gb. That
ignores KV cache for 1M-context models; rerun.py fixes those failures.
Lanes split items round-robin; with --lanes 2 keep TP <= 4.
"""

import argparse
import json
import subprocess

GPU_GB = {"b200": 180, "h200": 141}
FAMILY = {"b200": ["nvidia", "blackwell", "b200"], "h200": ["nvidia", "hopper", "h200"]}
SAME_FAMILY = {
    "b200": {"b200", "b300", "gb200", "gb300"},
    "h200": {"h200", "h100", "h20"},
}


def fetch(model):
    # curl, not urllib: python's SSL store is broken on some macOS installs
    out = subprocess.run(
        ["curl", "-sfL", "--max-time", "60", f"https://recipes.vllm.ai/{model}.json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def overrides(d, hw):
    out = {}
    for key in FAMILY[hw]:  # most specific wins
        if isinstance(d, dict) and key in d:
            out = d[key]
    return out


def render(r, hw, vname="default"):
    m, v = r["model"], r.get("variants", {}).get(vname, {})
    model_id = v.get("model_id") or m["model_id"]
    args, env = list(m.get("base_args", [])), dict(m.get("base_env", {}))
    for o in (
        v,
        overrides(v.get("hardware_overrides", {}), hw),
        overrides(r.get("hardware_overrides", {}), hw),
    ):
        args += o.get("extra_args", [])
        env.update(o.get("extra_env", {}))
    option = {
        f if isinstance(f, str) else (f.get("key") or f.get("name"))
        for f in r.get("opt_in_features", [])
    }
    for name, f in r.get("features", {}).items():
        if name not in option and isinstance(f, dict) and f.get("args"):
            args += overrides(f.get("hardware_overrides", {}), hw).get(
                "args", f["args"]
            )
    rec = r.get("recommended_command", {})
    env.update(rec.get("env", {}))
    vram = v.get("vram_minimum_gb") or 0
    tp = 1
    while tp < 8 and tp * GPU_GB[hw] < vram:
        tp *= 2
    if (
        vname == "default"
        and rec.get("hardware") in SAME_FAMILY[hw]
        and rec.get("argv")
    ):
        args = rec["argv"][3:]  # drop "vllm serve <model>"
        if "--tensor-parallel-size" in args:
            i = args.index("--tensor-parallel-size")
            del args[i : i + 2]
    if "--tensor-parallel-size" not in args:
        args += ["--tensor-parallel-size", str(tp)]
    return {
        "model": model_id,
        "variant": vname,
        "args": args,
        "env": env,
        "tp": tp,
        "vram": vram,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hw", required=True, choices=GPU_GB)
    ap.add_argument("--lanes", type=int, default=1, choices=(1, 2))
    ap.add_argument("--out", required=True)
    ap.add_argument("models", nargs="+")
    a = ap.parse_args()
    lanes = [[] for _ in range(a.lanes)]
    for i, spec in enumerate(a.models):
        model, _, variant = spec.partition(":")
        x = render(fetch(model), a.hw, variant or "default")
        lanes[i % a.lanes].append(x)
        print(f"lane {i % a.lanes} tp{x['tp']} {x['model']} " + " ".join(x["args"]))
    with open(a.out, "w") as f:
        json.dump(lanes, f, indent=1)

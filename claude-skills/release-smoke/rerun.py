#!/usr/bin/env python3
"""Rerun smoke-test startup failures with the fix the error asks for.

usage: sudo python3 rerun.py PLAN.json IMAGE HF_HOME OUTDIR

Waits for runner.py to write "ALL DONE" to OUTDIR/runner.log, then for each
startup failure:
  "KV cache is needed"  -> double TP (max 8); at TP8, add --max-model-len from
                           vLLM's own estimate (capped at 262144)
  "does not support current device" with --moe-backend -> drop --moe-backend
and runs those items through runner.py into OUTDIR/rerun/.
"""

import json
import os
import re
import subprocess
import sys
import time

MAX_LEN_CAP = 262144

plan_path, image, hf_home, out_dir = sys.argv[1:5]


def read(path):
    with open(path) as f:
        return f.read()


def main_run_done():
    path = f"{out_dir}/runner.log"
    return os.path.exists(path) and "ALL DONE" in read(path)


def failure_text(result):
    prefix = "smoke-"
    stem = result["model"].split("/")[-1][:30]
    logs = [f for f in os.listdir(out_dir) if f.startswith(prefix) and stem in f]
    return (result.get("log_tail") or "") + (
        read(f"{out_dir}/{logs[0]}") if logs else ""
    )


def fixed(item, text):
    args = list(item["args"])
    if "KV cache is needed" in text:
        if item["tp"] < 8:
            tp = item["tp"] * 2
            args[args.index("--tensor-parallel-size") + 1] = str(tp)
            return {
                **item,
                "tp": tp,
                "args": args,
                "variant": f"{item['variant']} (rerun: tp{tp})",
            }
        m = re.search(r"estimated maximum model length is (\d+)", text)
        max_len = min(MAX_LEN_CAP, int(m.group(1)) // 1024 * 1024) if m else 65536
        args += ["--max-model-len", str(max_len)]
        return {
            **item,
            "args": args,
            "variant": f"{item['variant']} (rerun: max-model-len {max_len})",
        }
    if "does not support current device" in text and "--moe-backend" in args:
        i = args.index("--moe-backend")
        del args[i : i + 2]
        return {
            **item,
            "args": args,
            "variant": f"{item['variant']} (rerun: no --moe-backend)",
        }
    return None


while not main_run_done():
    time.sleep(60)

with open(plan_path) as f:
    items = {x["model"]: x for lane in json.load(f) for x in lane}
redo = []
for line in read(f"{out_dir}/results.jsonl").splitlines():
    result = json.loads(line)
    if result["status"] == "FAIL" and result.get("stage") == "startup":
        item = fixed(items[result["model"]], failure_text(result))
        if item:
            redo.append(item)

with open(f"{out_dir}/runner.log", "a") as f:
    f.write(
        f"RERUN: {[x['model'] + ' ' + x['variant'] for x in redo] or 'nothing to rerun'}\n"
    )
if redo:
    rerun_plan = os.path.join(
        os.path.dirname(os.path.abspath(out_dir)), "plan_rerun.json"
    )
    with open(rerun_plan, "w") as f:
        json.dump([redo], f, indent=1)
    runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runner.py")
    subprocess.run(
        ["python3", runner, rerun_plan, image, hf_home, f"{out_dir}/rerun"], check=False
    )

#!/usr/bin/env python3
"""Smoke-test a vLLM release image against a plan from make_plan.py.

usage: sudo python3 runner.py PLAN.json IMAGE HF_HOME OUTDIR

For each model: `hf download` inside the image, `docker run` the release image
with the recipe's serve args, wait for /health (up to 90 min, bail if the
container exits), GET /v1/models, send one chat completion ("What is 2+2?"),
save the server log, tear down. A two-lane plan runs on GPUs 0-3 and 4-7 in
parallel. Results append to OUTDIR/results.jsonl; progress goes to
OUTDIR/runner.log, which ends with "ALL DONE".
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HEALTH_TIMEOUT_S = 90 * 60
DOWNLOAD_TIMEOUT_S = 6 * 3600
PROMPT = "What is 2+2? Reply with just the number."

plan_path, image, hf_home, out_dir = sys.argv[1:5]
with open(plan_path) as f:
    plan = json.load(f)
os.makedirs(out_dir, exist_ok=True)
lock = threading.Lock()


def log(msg):
    with lock, open(f"{out_dir}/runner.log", "a") as f:
        f.write(time.strftime("%H:%M:%S ") + msg + "\n")


def record(result):
    with lock, open(f"{out_dir}/results.jsonl", "a") as f:
        f.write(json.dumps(result) + "\n")


def sh(cmd, timeout=None):
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def http(url, data=None, timeout=60):
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode()


def download(model):
    start = time.time()
    r = sh(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            "-e",
            f"HF_HOME={hf_home}",
            "-v",
            f"{hf_home}:{hf_home}",
            "--entrypoint",
            "hf",
            image,
            "download",
            model,
        ],
        timeout=DOWNLOAD_TIMEOUT_S,
    )
    err = r.stderr.strip()[-200:] if r.returncode else ""
    log(f"download {model}: rc={r.returncode} {time.time() - start:.0f}s {err}")
    return r.returncode == 0


def draft_models(args):
    """Speculative-config draft checkpoints that also need downloading."""
    for a in args:
        if a.startswith("{") and '"model"' in a:
            try:
                yield json.loads(a)["model"]
            except (ValueError, KeyError):
                continue


def wait_healthy(name, port):
    start = time.time()
    while time.time() - start < HEALTH_TIMEOUT_S:
        running = sh(
            ["docker", "inspect", "-f", "{{.State.Running}}", name]
        ).stdout.strip()
        if running != "true":
            return False
        try:
            if http(f"http://127.0.0.1:{port}/health", timeout=5)[0] == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass  # not up yet
        time.sleep(15)
    return False


def chat(port):
    _, models = http(f"http://127.0.0.1:{port}/v1/models")
    served = json.loads(models)["data"][0]["id"]
    payload = {
        "model": served,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 2048,
        "temperature": 0,
    }
    _, body = http(f"http://127.0.0.1:{port}/v1/chat/completions", payload, timeout=600)
    msg = json.loads(body)["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    return {
        "status": "PASS" if "4" in content else "CHECK",
        "answer": content[:200],
        "reasoning_len": len(reasoning),
    }


def run_one(lane_idx, item, gpus, port):
    model = item["model"]
    name = f"smoke-l{lane_idx}-{model.split('/')[-1][:30]}"
    result = {
        "model": model,
        "variant": item["variant"],
        "tp": item["tp"],
        "gpus": gpus,
    }
    start = time.time()
    if not download(model):
        return {**result, "status": "FAIL", "stage": "download"}
    for draft in draft_models(item["args"]):
        download(draft)
    sh(["docker", "rm", "-f", name])
    devices = ",".join(str(g) for g in gpus[: item["tp"]])
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--gpus",
        f'"device={devices}"',
        "--ipc=host",
        "--network",
        "host",
        "--shm-size",
        "32g",
        "-e",
        f"HF_HOME={hf_home}",
        "-v",
        f"{hf_home}:{hf_home}",
        "-e",
        "VLLM_ENGINE_READY_TIMEOUT_S=3600",
    ]
    for k, v in item["env"].items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [image, model, *item["args"], "--port", str(port)]
    r = sh(cmd)
    if r.returncode:
        return {
            **result,
            "status": "FAIL",
            "stage": "docker run",
            "err": r.stderr[-300:],
        }
    log(f"lane{lane_idx} started {model} on GPUs {devices} port {port}")
    healthy_start = time.time()
    healthy = wait_healthy(name, port)
    result["startup_s"] = round(time.time() - healthy_start)
    if not healthy:
        tail = sh(["docker", "logs", "--tail", "60", name])
        result.update(
            status="FAIL", stage="startup", log_tail=(tail.stdout + tail.stderr)[-6000:]
        )
    else:
        try:
            result.update(chat(port))
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as e:
            result.update(status="FAIL", stage="request", err=str(e)[:300])
    with open(f"{out_dir}/{name}.log", "w") as f:
        subprocess.run(
            ["docker", "logs", name], stdout=f, stderr=subprocess.STDOUT, check=False
        )
    sh(["docker", "rm", "-f", name])
    result["total_s"] = round(time.time() - start)
    return result


def run_lane(lane_idx, lane, gpus, port):
    for item in lane:
        result = run_one(lane_idx, item, gpus, port)
        record(result)
        log(
            f"lane{lane_idx} {item['model']}: {result['status']} ({result.get('stage', '')}) startup {result.get('startup_s')}s"
        )


def prefetch():
    for lane in plan:
        for item in lane:
            download(item["model"])


threading.Thread(target=prefetch, daemon=True).start()
lane_gpus = (
    [([0, 1, 2, 3], 8101), ([4, 5, 6, 7], 8102)]
    if len(plan) == 2
    else [(list(range(8)), 8101)]
)
threads = [
    threading.Thread(target=run_lane, args=(i, plan[i], *lane_gpus[i]))
    for i in range(len(plan))
]
for t in threads:
    t.start()
for t in threads:
    t.join()
log("ALL DONE")

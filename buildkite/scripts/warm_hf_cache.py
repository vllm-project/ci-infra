"""Pre-download frequently-flaking test models/datasets into the shared HF cache.

Runs inside the vLLM CI test image (same huggingface_hub/mteb/datasets
versions as the test steps) with HF_HOME pointed at the shared cache mount
(/fsx/hf_cache on the GPU queues). huggingface_hub's file locking makes this
safe to run while test jobs read/write the same cache.

Environment:
  HF_HOME             Cache directory (set by the caller to the shared mount).
  HF_TOKEN            Optional token; all default repos are ungated.
  WARM_MODELS         Space-separated HF repo ids to snapshot (default: the
                      bitsandbytes test set below). Failures fail the job.
  WARM_MTEB           "1" (default) to also warm the MTEB STS12/NFCorpus
                      datasets and bm25s via the mteb loader. Failures fail
                      the job.
  WARM_GSM8K_MODELS   Space-separated HF repo ids for the large GSM8K eval
                      models (default: empty). These are tens of GB each;
                      failures only warn so a big-model blip doesn't block
                      the small-model warm.
"""

import os
import sys
import time

# Models used by tests/plugins_tests/bitsandbytes/ (all 12 tests failed on the
# same 32 runs in the 28-day flake window — shared HF download cause). The
# gated meta-llama/* originals are intentionally excluded; the vllm test PRs
# swap them for these ungated equivalents.
DEFAULT_WARM_MODELS = [
    "facebook/opt-125m",
    "poedator/opt-125m-bnb-4bit",
    "yec019/fbopt-350m-8bit",
    "mistralai/Mistral-7B-Instruct-v0.3",
    "unsloth/tinyllama-bnb-4bit",
    "unsloth/Llama-3.2-1B-Instruct",
    "PrunaAI/Einstein-v6.1-Llama3-8B-bnb-4bit-smashed",
]

MTEB_TASKS = ["STS12", "NFCorpus"]
MTEB_MODELS = ["bm25s"]

ATTEMPTS = 4
BACKOFF_SECONDS = [10, 30, 90]


def download_with_retries(repo_id, warn_only=False):
    from huggingface_hub import snapshot_download

    for attempt in range(ATTEMPTS):
        try:
            path = snapshot_download(repo_id)
            print(f"OK   {repo_id} -> {path}", flush=True)
            return True
        except Exception as exc:  # noqa: BLE001 - any hub/network error is retryable here
            if attempt < ATTEMPTS - 1:
                delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                print(
                    f"RETRY {repo_id} (attempt {attempt + 1}/{ATTEMPTS}): {exc!r}; "
                    f"sleeping {delay}s",
                    flush=True,
                )
                time.sleep(delay)
            else:
                level = "WARN" if warn_only else "FAIL"
                print(f"{level} {repo_id}: gave up after {ATTEMPTS} attempts: {exc!r}",
                      flush=True)
                return False


def warm_mteb():
    """Warm MTEB eval datasets/models so pooling_mteb steps never hit the hub."""
    try:
        import mteb
    except ImportError:
        print("FAIL mteb package not importable in the warm image", flush=True)
        return False

    ok = True
    for model_name in MTEB_MODELS:
        try:
            mteb.get_model(model_name)
            print(f"OK   mteb model {model_name}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL mteb model {model_name}: {exc!r}", flush=True)
            ok = False
    for task_name in MTEB_TASKS:
        try:
            for task in mteb.get_tasks(
                tasks=[task_name], languages=["eng"], eval_splits=["test"]
            ):
                task.load_data()
            print(f"OK   mteb task {task_name}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL mteb task {task_name}: {exc!r}", flush=True)
            ok = False
    return ok


def main():
    print(f"HF_HOME={os.environ.get('HF_HOME', '<unset>')}", flush=True)

    failures = []

    models = os.environ.get("WARM_MODELS", "").split() or DEFAULT_WARM_MODELS
    print(f"Warming {len(models)} model repo(s)...", flush=True)
    for repo_id in models:
        if not download_with_retries(repo_id):
            failures.append(repo_id)

    if os.environ.get("WARM_MTEB", "1") == "1":
        print("Warming MTEB datasets/models...", flush=True)
        if not warm_mteb():
            failures.append("mteb")

    gsm8k_models = os.environ.get("WARM_GSM8K_MODELS", "").split()
    if gsm8k_models:
        print(f"Warming {len(gsm8k_models)} GSM8K eval model(s) (warn-only)...",
              flush=True)
        for repo_id in gsm8k_models:
            download_with_retries(repo_id, warn_only=True)

    if failures:
        print(f"Cache warm FAILED for: {', '.join(failures)}", flush=True)
        return 1
    print("Cache warm complete.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

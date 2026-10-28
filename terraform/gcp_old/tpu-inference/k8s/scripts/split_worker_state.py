#!/usr/bin/env python3
"""Split the workers out of manager/'s state, into their environments' states.

manager/ built both workers before they had an environment of their own. Their
resources cannot follow them with moved blocks, which only work within one
state, and importing them does not round-trip: the bucket comes back without
its project (which forces replacement) and labels and provider-only fields come
back differently from how they were created. `terraform state mv` carries every
attribute as recorded, so the environments plan no changes.

  ./scripts/split_worker_state.py OUT_DIR

Pulls manager/'s state, moves each worker's resources into
OUT_DIR/<environment>.tfstate under the addresses its module block in
workers/<environment>/ gives them, and prints the `state push` that puts each
file in its environment's backend. It pushes nothing itself, and never writes
manager/'s state: manager/removed.tf forgets the same resources there when
manager/ is applied.

A push goes to a backend nothing has written to yet, so it cannot overwrite
anything. manager/ and the environments may be applied in either order: in
between, a resource is in both states or in neither, and neither plan deletes
it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from generate_manifests import ROOT, find_workers, load_tfvars

MANAGER = ROOT / "manager"

# Old address in manager/ -> address under the worker's module block. The old
# key is "<project>/<location>" for the cluster-level resources and that plus
# "/<rest>" for the per-pool and per-bucket ones; the new key is <rest>.
MOVES = {
    ("google_container_cluster", "worker"): "google_container_cluster.this",
    ("google_container_node_pool", "worker_system"): "google_container_node_pool.system",
    ("google_container_node_pool", "worker_tpu"): "google_container_node_pool.tpu",
    ("google_compute_resource_policy", "tpu_slice"): "google_compute_resource_policy.tpu_slice",
    ("google_service_account", "worker_nodes"): "google_service_account.nodes",
    ("google_project_iam_member", "worker_nodes"): "google_project_iam_member.nodes",
    ("google_storage_bucket", "workload"): "google_storage_bucket.workload",
    ("google_storage_bucket_iam_member", "workload"): "google_storage_bucket_iam_member.workload",
    ("google_storage_anywhere_cache", "workload"): "google_storage_anywhere_cache.workload",
}

ADDRESS = re.compile(r'(\w+)\.(\w+)\["([^"]+)"\]')


def run(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode:
        raise SystemExit(f"failed: {' '.join(args)}\n{proc.stderr}")
    return proc.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # From the old key, <project>/<location>, to the environment and module
    # label that now declare that worker.
    declared = find_workers()
    worker_by_key = {}
    for name, w in load_tfvars(MANAGER / "prod.auto.tfvars")["workers"].items():
        env, label = declared[name]["_env"], declared[name]["_label"]
        worker_by_key[f"{w['project']}/{w['location']}"] = (env, label)

    source = out_dir / "manager.tfstate"
    source.write_text(run("terraform", "state", "pull", cwd=MANAGER))

    moves: dict[str, list[tuple[str, str]]] = {}
    for address in run("terraform", "state", "list", f"-state={source}",
                       cwd=out_dir).split():
        match = ADDRESS.fullmatch(address)
        if not match:
            continue
        rtype, name, key = match.groups()
        target = MOVES.get((rtype, name))
        if not target:
            continue
        parts = key.split("/")
        env, label = worker_by_key["/".join(parts[:2])]
        rest = "/".join(parts[2:])
        new = f"module.{label}.{target}" + (f'["{rest}"]' if rest else "")
        moves.setdefault(env, []).append((address, new))

    # From an empty directory, so that state mv reads only the files it is
    # given and not a backend configured wherever this was run from.
    with tempfile.TemporaryDirectory() as scratch:
        for env, pairs in sorted(moves.items()):
            out = out_dir / f"{env}.tfstate"
            if out.exists():
                raise SystemExit(f"{out} exists; start from an empty OUT_DIR")
            for old, new in pairs:
                # The backups state mv insists on, kept out of OUT_DIR so that
                # what is left there is the files to push.
                run("terraform", "state", "mv", f"-state={source}",
                    f"-state-out={out}", f"-backup={scratch}/source.backup",
                    f"-backup-out={scratch}/out.backup", old, new,
                    cwd=Path(scratch))
            print(f"{env}: {len(pairs)} resources -> {out}", file=sys.stderr)

    print("\nThen, for each environment:", file=sys.stderr)
    for env in sorted(moves):
        print(f"  terraform -chdir=workers/{env} init && "
              f"terraform -chdir=workers/{env} state push {out_dir / (env + '.tfstate')}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

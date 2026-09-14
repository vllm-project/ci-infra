#!/usr/bin/env python3
"""Install Kueue and JobSet on every cluster in prod.auto.tfvars and apply this
fleet's configuration on top.

Not Terraform: the Kubernetes and Helm providers need a reachable API server at
plan time, which turns creating a cluster and configuring it into two separate
runs with a hand-edited variable between them. Here the clusters Terraform built
are the input and every apply is idempotent, so this is safe to re-run and the
plan is `kubectl diff`.

  pip install -r scripts/requirements.txt

  ./scripts/deploy_manifests.py               # diff, then confirm before applying
  ./scripts/deploy_manifests.py --mode diff   # show what would change and stop
  ./scripts/deploy_manifests.py --mode apply  # no preview, no prompt
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from generate_manifests import (
    DEFAULT_OUT,
    LAUNCHER_DEFAULT_JOB,
    LAUNCHER_DEFAULT_JOB_KEY,
    LAUNCHER_MANIFEST_CONFIGMAP,
    LAUNCHER_PREWARM_JOB,
    LAUNCHER_PREWARM_JOB_KEY,
    LAUNCHER_SCRIPT,
    LAUNCHER_SCRIPT_CONFIGMAP,
    LAUNCHER_SCRIPT_KEY,
    TFVARS,
    generate,
    load_tfvars,
)

# Server-side apply records which manager owns which field, and removes any
# field a manager owned and then stopped sending. So the upstream release and
# our overlay of it cannot go on under one name: applied as "kubectl", the
# auth-plugin overlay reads as that manager's new and complete idea of the
# Deployment, and strips every field the upstream apply had just set - selector,
# image, template labels. Upstream keeps kubectl's default, so that applying the
# same release by hand behaves the same way; ours is its own.
FIELD_MANAGER = "tpu-ci"
UPSTREAM_FIELD_MANAGER = "kubectl"

UPSTREAM_MANIFESTS = {
    "kueue": "https://github.com/kubernetes-sigs/kueue/releases/download/v{version}/manifests.yaml",
    "jobset": "https://github.com/kubernetes-sigs/jobset/releases/download/v{version}/manifests.yaml",
}

AGENT_STACK_CHART = "oci://ghcr.io/buildkite/helm/agent-stack-k8s"


def upstream(name: str, index: dict) -> str:
    return UPSTREAM_MANIFESTS[name].format(version=index[f"{name}_version"])


def run_command(*args: str, quiet: bool = False) -> None:
    """Run a command, or stop. There is no recovering from a failure here: what
    we would be continuing from is a half-configured cluster."""
    stdout = subprocess.DEVNULL if quiet else None
    if subprocess.run(args, stdout=stdout).returncode:
        raise SystemExit(f"failed: {shlex.join(args)}")


def ssa_flags(field_manager: str) -> list[str]:
    """Server-side is mandatory, not a preference: the workloads and jobsets
    CRDs are each over a megabyte, and a client-side apply stores the whole
    object in the last-applied-configuration annotation, capped at 256 KB.

    --force-conflicts is how our ConfigMap override survives. Applying an
    upstream release hands ownership of every field it sets back to upstream;
    taking it back is the point of applying ours afterwards. Field ownership is
    per list item, so the auth plugin's container, volume and mount collide with
    nothing.
    """
    return ["--server-side", "--force-conflicts", f"--field-manager={field_manager}"]


class Step:
    """One kubectl apply, and the preview of that same apply.

    What varies between the steps below is only where the YAML comes from -
    committed, rendered by helm, built by kubectl - and that is deliberately the
    only thing that varies. There is one way of applying things and one way of
    previewing them, so a step that produces its manifest at deploy time is
    still previewed by `kubectl diff` against the same server-side merge the
    apply will do.
    """

    field_manager: str

    @contextmanager
    def _target(self):
        """The -f arguments naming what to apply, for the length of one step."""
        raise NotImplementedError

    def run(self) -> None:
        with self._target() as target:
            run_command("kubectl", "apply", *ssa_flags(self.field_manager), *target)

    def diff(self) -> bool:
        """Preview it. False if the preview could not be produced.

        A failure is not fatal. Before the first install there is no
        kueue-system namespace to diff namespaced objects against, so diff
        cannot work and there is nothing yet to be careful about; afterwards a
        failure is worth seeing but the apply would report it too. Either way
        the operator is told the preview is incomplete before being asked to
        confirm.
        """
        # The same flags as the apply, so the preview merges the way the apply
        # will. A client-side diff would have shown the auth-plugin overlay as a
        # clean three-field addition and said nothing about the fields it was
        # going to take off the Deployment.
        #
        # kubectl diff exits 1 when there are differences and above that when it
        # failed, so only the second is a problem.
        with self._target() as target:
            cmd = ["kubectl", "diff", *ssa_flags(self.field_manager), *target]
            if subprocess.run(cmd).returncode > 1:
                # On stderr, next to the error it is explaining.
                print("  (could not diff the above; nothing installed yet, "
                      "or the cluster rejected it)", file=sys.stderr)
                return False
        return True


@dataclass(frozen=True)
class Apply(Step):
    """A committed manifest, or an upstream release fetched by URL."""

    what: str
    source: str | Path
    field_manager: str = FIELD_MANAGER

    @contextmanager
    def _target(self):
        # An upstream release is one file; ours are directories.
        if isinstance(self.source, Path):
            yield ["-R", "-f", str(self.source)]
        else:
            yield ["-f", self.source]


@dataclass(frozen=True)
class Chart(Step):
    """An upstream Helm chart, rendered and then treated as any other manifest.

    helm is a renderer here and nothing else. There is no release, no history
    and no in-cluster Helm state, so `helm template` output goes through the
    same server-side apply as everything else.

    Not rendered at generate time into the committed tree, tempting as that is
    for reviewability: the output would then depend on the helm binary that
    happened to render it, and a colleague on another helm version would fail
    the drift check having changed nothing. The values are committed instead,
    which is the part we actually write.
    """

    what: str
    # The release name, which is not cosmetic: the chart builds every object's
    # name from it, so changing it renames the Deployment rather than updating
    # it. It is the chart's own name because that is what upstream's install
    # instructions use, and the names in `kubectl get` should match theirs.
    release: str
    chart: str
    version: str
    namespace: str
    values: Path
    field_manager: str = FIELD_MANAGER

    @contextmanager
    def _target(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Fetched and rendered as two commands rather than one. helm prints
            # what it pulled from an OCI registry on stdout, in the middle of
            # the YAML, and kubectl reads that as a document with no kind; this
            # way the notice belongs to the fetch and only the render is parsed.
            self._helm("pull", self.chart, "--version", self.version,
                       "--destination", tmp)
            [archive] = Path(tmp).glob("*.tgz")

            out = Path(tmp) / "rendered.yaml"
            out.write_text(self._helm(
                "template", self.release, str(archive),
                "--namespace", self.namespace, "--values", str(self.values),
            ))
            yield ["-f", str(out)]

    @staticmethod
    def _helm(*args: str) -> str:
        proc = subprocess.run(["helm", *args], capture_output=True, text=True)
        if proc.returncode:
            raise SystemExit(f"failed: {shlex.join(('helm',) + args)}\n{proc.stderr}")
        return proc.stdout


@dataclass(frozen=True)
class ConfigMapFile(Step):
    """Files on disk, applied as the keys of a ConfigMap.

    kubectl builds the object, so the file goes in verbatim - no template, no
    indentation, and no second copy that can drift from the first. The
    alternative is rendering it into the generated tree, which for the
    launcher's program would mean committing it twice: once as something that
    can be linted and run, and once as a thousand indented lines nobody reads a
    diff of.

    The cost is that this one object is not in kueue/generated/, so it is
    reviewed as a change to the file rather than as a change to YAML. For a
    program that is the better review anyway.
    """

    what: str
    name: str
    # Key to file, as a tuple of pairs rather than a dict: this dataclass is
    # frozen and a dict is not hashable. One kubectl invocation builds the whole
    # object, because a ConfigMap is applied whole - a second step naming the
    # same object would replace it and take the first one's keys with it.
    files: tuple
    namespace: str
    field_manager: str = FIELD_MANAGER

    @contextmanager
    def _target(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ["kubectl", "create", "configmap", self.name,
                    "--namespace", self.namespace,
                    *(f"--from-file={key}={path}" for key, path in self.files),
                    "--dry-run=client", "-o", "yaml"]
            proc = subprocess.run(args, capture_output=True, text=True)
            if proc.returncode:
                raise SystemExit(f"failed: {shlex.join(args)}\n{proc.stderr}")
            out = Path(tmp) / "configmap.yaml"
            out.write_text(proc.stdout)
            yield ["-f", str(out)]


@dataclass(frozen=True)
class Settle(Step):
    """Wait for the controllers the applies before it installed."""

    what: str = "wait for the CRDs and the controllers"

    def run(self) -> None:
        run_command("kubectl", "wait", "--for=condition=Established", "--timeout=180s",
                    "crd/clusterqueues.kueue.x-k8s.io",
                    "crd/localqueues.kueue.x-k8s.io",
                    "crd/resourceflavors.kueue.x-k8s.io",
                    "crd/jobsets.jobset.x-k8s.io")

        # Picks up both the ConfigMap and the plugin: a mounted ConfigMap
        # changing does not restart anything by itself.
        run_command("kubectl", "rollout", "restart",
                    "deployment/kueue-controller-manager", "-n", "kueue-system")
        run_command("kubectl", "rollout", "status", "deployment/kueue-controller-manager",
                    "-n", "kueue-system", "--timeout=300s")
        run_command("kubectl", "rollout", "status", "deployment/jobset-controller-manager",
                    "-n", "jobset-system", "--timeout=300s")

    def diff(self) -> bool:
        # Nothing to preview: it changes nothing, and what it waits for may not
        # exist until the applies above it have run.
        return True


def plan(cluster: dict, index: dict) -> list[Step]:
    """The install order, written once.

    The preview walks this same list, in this same order, which is the point of
    it being a list: a preview that walks the tree its own way reads objects
    before the namespace and CRDs they need, aborts, and reports clean on an
    overlay that would have deleted half a Deployment.
    """
    base = DEFAULT_OUT / cluster["dir"]
    steps: list[Step] = [
        # JobSet first: Kueue only enables its jobset integration for a CRD that
        # exists when the controller starts, and the rollout below is what makes
        # that true on a fresh cluster.
        Apply("JobSet release", upstream("jobset", index), UPSTREAM_FIELD_MANAGER),
        Apply("Kueue release", upstream("kueue", index), UPSTREAM_FIELD_MANAGER),

        # Immediately after, so the window in which the controller could come up
        # on upstream's default configuration - no MultiKueue, quota checks
        # blocking on undeclared resources - is as short as possible.
        Apply("fleet configuration", base / "system"),

        Settle(),

        # Before the workload directory, because ClusterQueue and LocalQueue go
        # through Kueue's validating webhook - the controller that just
        # restarted - and because the namespace everything below lives in is
        # created here.
        Apply("queues", base / "queues"),
    ]

    # Before the workload directory, whose PodTemplate mounts it. Manager only,
    # keyed off the launcher manifest being rendered for this cluster.
    if (base / "workload" / "10-launcher.yaml").is_file():
        steps.append(ConfigMapFile(
            "launcher program",
            name=LAUNCHER_SCRIPT_CONFIGMAP,
            files=((LAUNCHER_SCRIPT_KEY, LAUNCHER_SCRIPT),),
            namespace=index["namespace"],
        ))
        steps.append(ConfigMapFile(
            "launcher job manifests",
            name=LAUNCHER_MANIFEST_CONFIGMAP,
            files=(
                (LAUNCHER_DEFAULT_JOB_KEY, LAUNCHER_DEFAULT_JOB),
                (LAUNCHER_PREWARM_JOB_KEY, LAUNCHER_PREWARM_JOB),
            ),
            namespace=index["namespace"],
        ))

    # What the namespace needs beyond its queues, which is not the same on both
    # sides: the manager syncs the agent token and runs the launcher, a worker
    # gets the identity its TPU pods run as and the caches they mount.
    if (base / "workload").is_dir():
        steps.append(Apply("workload identity, secrets and caches", base / "workload"))

    # Last, and manager only. The controller looks its agent token up by name at
    # startup, so it wants the Secret that workload/ creates to be there
    # already; started first it would crash-loop until the SecretSync caught up.
    values = base / "charts" / "agent-stack-k8s.yaml"
    if values.is_file():
        steps.append(Chart(
            "Buildkite controller",
            release="agent-stack-k8s",
            chart=AGENT_STACK_CHART,
            version=index["agent_stack_version"],
            namespace=index["namespace"],
            values=values,
        ))

    return steps


def deploy_cluster(cluster: dict, index: dict, preview: bool) -> bool:
    """Configure one cluster, or preview it. False if the preview was partial."""
    print(f"\n=== {cluster['name']} ({cluster['location']}, {cluster['role']}) ===")

    # Only stdout is dropped. A failure here would otherwise leave the previous
    # cluster selected and quietly apply one cluster's manifests to another.
    run_command("gcloud", "container", "clusters", "get-credentials", cluster["name"],
                "--project", cluster["project"], "--location", cluster["location"],
                quiet=True)

    steps = plan(cluster, index)
    complete = True
    for position, step in enumerate(steps, start=1):
        print(f"\n-- {position}/{len(steps)} {step.what}")
        if not preview:
            step.run()
        # Not `complete and step.diff()`: every step should be previewed, and
        # short-circuiting would drop the rest after the first one that failed.
        elif not step.diff():
            complete = False
    return complete


def verify_generated() -> dict:
    """Re-render the manifests, and refuse to deploy a committed tree that has
    drifted from them.

    A stale generated/ would silently deploy the previous tfvars, and the diff
    below would look clean. The cluster list comes back from that same render
    rather than from a file of its own, so what is deployed and what was checked
    cannot describe different fleets.
    """
    with tempfile.TemporaryDirectory() as fresh:
        index = generate(load_tfvars(TFVARS), Path(fresh))
        if subprocess.run(["diff", "-ru", str(DEFAULT_OUT), fresh]).returncode:
            raise SystemExit(
                "\nkueue/generated is out of date: "
                "run scripts/generate_manifests.py and commit."
            )
    return index


def verify_buckets() -> None:
    """Every bucket a PersistentVolume names has to exist already.

    generate_manifests.py derives the name and locals.tf derives it again to
    create the bucket, so the two can drift - and Kubernetes will not notice. A
    volume naming a bucket nobody created applies cleanly and keeps working
    until a pod tries to mount it, which is somewhere else, later, and reads as
    a broken pod rather than a broken name. Terraform runs before this script,
    so by now the bucket either exists or the name is wrong.

    Read by line rather than parsed: this tree is generated, so the spelling is
    ours, and it saves the script a YAML dependency it otherwise does not need.
    """
    handles = sorted({
        line.split(":", 1)[1].strip()
        for path in DEFAULT_OUT.rglob("*.yaml")
        for line in path.read_text().splitlines()
        if line.strip().startswith("volumeHandle:")
    })
    missing = [
        bucket for bucket in handles
        if subprocess.run(
            ["gcloud", "storage", "buckets", "describe", f"gs://{bucket}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode
    ]
    if missing:
        raise SystemExit(
            f"\nno such bucket: {', '.join(missing)}\n"
            "A PersistentVolume names it, so either terraform has not been "
            "applied yet, or locals.tf and generate_manifests.py no longer "
            "derive the same name."
        )
    print(f"{len(handles)} cache bucket(s) exist.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # One choice rather than two booleans, because the preview and the apply are
    # the same walk over the same files - what varies is only how much of it to
    # do, which is one question with three answers and not two with four.
    parser.add_argument("--mode", choices=("diff", "confirm", "apply"),
                        default="confirm",
                        help="diff: show what would change and stop. "
                             "confirm: diff, then ask before applying (default). "
                             "apply: skip the preview and the prompt.")
    args = parser.parse_args()

    # Our output is interleaved with kubectl's and gcloud's, which write to the
    # terminal directly. Left buffered, every heading here arrives after the
    # output it was meant to introduce.
    sys.stdout.reconfigure(line_buffering=True)

    for tool in ("kubectl", "gcloud", "helm"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} is not on PATH")

    # Do not touch the operator's own kubeconfig or leave a context selected.
    with tempfile.TemporaryDirectory() as kubeconfig_dir:
        os.environ["KUBECONFIG"] = str(Path(kubeconfig_dir) / "config")

        index = verify_generated()
        verify_buckets()
        # Workers first, manager last. What the manager holds is the launcher's
        # registry, and everything in it is a promise about a worker - the
        # queues a shape can be admitted to, the Secret a forwarded credential
        # resolves against. A manager updated ahead of its workers is one
        # promising things that are not there yet, and the pod that discovers
        # it does so holding a TPU reservation.
        clusters = sorted(index["clusters"], key=lambda c: c["role"] == "manager")
        print(f"Kueue v{index['kueue_version']}, JobSet v{index['jobset_version']}, "
              f"agent-stack-k8s v{index['agent_stack_version']}, "
              f"{len(clusters)} cluster(s).")

        if args.mode != "apply":
            # A list, not a generator: every cluster should be previewed, and
            # all() would stop at the first one whose preview was partial.
            complete = all([
                deploy_cluster(c, index, preview=True) for c in clusters
            ])
            print()
            if not complete:
                print("Part of the preview above could not be produced; "
                      "see the notes above.")

        if args.mode == "confirm":
            if input(f"Apply to all {len(clusters)} cluster(s)? [y/N] ").lower() != "y":
                raise SystemExit("aborted")

        if args.mode != "diff":
            for cluster in clusters:
                deploy_cluster(cluster, index, preview=False)

    print("\ndone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

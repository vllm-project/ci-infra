# Inferact TPU7x Buildkite pools

This root creates 32 physical chips in `inferact-vllm-tpu`, `us-central1-c`.
It uses the Compute Engine reservation `ghostfish-9mpeile911sjq`; the sibling
`ci_v7x` module uses the legacy Cloud TPU API and cannot consume this reservation.
The Compute module reuses the shared agent watchdog and GitHub App integration.

| Target queue | Slices / agents | Hosts per slice | Chips per slice | JAX devices per job |
| --- | ---: | ---: | ---: | ---: |
| tpu_v7x_16_queue | 2 | 2 | 8 | 16 |
| tpu_v7x_8_queue | 4 | 1 | 4 | 8 |

Each host has four physical chips and eight JAX devices. Only host `-001`
starts an agent. Two-host slices use a `2x2x2` workload policy and BULK creation.
Single-host slices are independent. One concurrent job owns its entire slice.

## State and shared dependencies

State: `gs://inferact-terraform-states/inferact-vllm-tpu/buildkite-pools`.
The existing `buildkite-kevin-test` state continues to own the private network,
subnet, NAT, firewall, service account and Secret Manager bootstrap secret.
**Do not destroy that root** when retiring the old four-host TPU slice: retain
those shared resources. This root neither modifies nor deletes that slice.
No Buildkite token or private SSH key is embedded in Terraform metadata/state;
VMs fetch the existing bootstrap secret with their service account.

Each VM has a 1 TiB boot disk. `/mnt/disks/persist` is a directory on that disk;
it survives reboot but not VM replacement. It is not a separately retained disk.
Operator access uses OS Login / IAP on port 22. CI SSH uses port 2222.

## Validation and admission

Fresh instances start on `tpu_v7x_32_kevin_test`, tagged with `slice` and
`target_queue`. Canary steps must select both the validation queue and slice;
otherwise they might land on the original four-host test slice.
Run `TPU_RUN_TIMEOUT=600 tpu-run /opt/tpu-ci/venv/bin/python /opt/tpu-ci/smoke.py`
to verify local device count, global device count, matrix multiplication and
cross-host all-reduce. Check all six slices, plus launch failure/cancel cleanup.

Before admission, also verify real pipeline checkout and image/model access.
GitHub checkout uses cluster secret `GITHUB_CI_BOT_PEM`, app 4156238,
installation 142868369, with a repository-scoped read-only installation token.
Its job secret policy must permit the canary pipeline. Image registry and GCS
access use the VM service account, and need grants from the resource owners.
A hardware smoke pass alone is insufficient for production admission.

After validation, on each idle leader, create
`/etc/buildkite-agent/dispatch-enabled`, change only its `queue=` tag from the
validation queue to the configured target queue, and restart the agent.
The marker preserves admission across reboot. Replacement VMs lack the marker
and must be validated again. Do not restart a leader while it is running a job.

Apply only a reviewed plan in this root. Operator needs temporary Compute Admin
and Service Account User; Secret Manager Admin is not needed because the secret
and service-account access already exist. Remove temporary grants when finished.

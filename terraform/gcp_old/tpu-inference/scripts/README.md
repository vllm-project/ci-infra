# Rolling Restart Script for Terraform Instances

This directory contains `rolling_restart.py`, a helper script to perform rolling restarts/replaces on TPU and CPU instances managed by Terraform.

It replaces instances (and optionally their associated disks) in batches rather than tearing down everything at once. It uses Terraform's `-replace` flag to trigger recreation, combined with `-target` to ensure other configuration changes or drifts in the workspace are not applied.

## Features
- **Rolling Replacement**: Recreate instances in customizable batch sizes (default: 1).
- **Targeted Operations**: Avoid applying unrelated configuration drift using `-target`.
- **Target Specific Instances**: Recreate only specific instances by passing a comma-separated list of indexes (e.g. `-i 0,2,5`).
- **Disk Replacement Option**: Optionally replace the associated `google_compute_disk` along with each instance (using `-d`/`--replace-disks`).
- **Safety Delay**: Pause for a configured number of seconds between batches to allow new instances to register or boot up (using `--delay`).
- **Dry Run Support**: View the batches and the exact Terraform commands that will be executed without applying them (using `--dry-run`).

## Usage
Run the script from the directory containing your Terraform files, or use the `--dir` option:

```bash
./scripts/rolling_restart.py -m <MODULE_NAME> [options]
```

### Options
- `-m`, `--module`: (Required) The target Terraform module (e.g., `module.ci_v6e_1`).
- `-b`, `--batch-size`: Number of instances to replace per batch (default: `1`).
- `-d`, `--replace-disks`: Also replace associated disks (`google_compute_disk`) alongside the instances.
- `-i`, `--indexes`: Comma-separated list of specific instance indexes to replace (e.g., `0,2,5`).
- `--auto-approve`: Pass `-auto-approve` to the underlying `terraform apply` commands.
- `--delay`: Delay in seconds to wait between the completion of a batch recreate and the start of the next recreate (default: `0`).
- `--non-interactive`: Do not prompt for confirmation before starting each batch.
- `--dir`: Directory containing the target Terraform configuration (default: `.`).
- `--dry-run`: Print the commands that would be executed without running them.

---

## Examples

### 1. Dry Run a Rolling Restart
To inspect what batches and commands will be run without executing anything:
```bash
./scripts/rolling_restart.py -m module.ci_v6e_1 --dir cloud-ullm-inference-ci-cd --dry-run
```

### 2. Replace Instances One by One (Interactive)
Recreate all instances in `module.ci_v6e_1` one at a time, prompting you for confirmation before executing each step:
```bash
./scripts/rolling_restart.py -m module.ci_v6e_1 --dir cloud-ullm-inference-ci-cd
```

### 3. Replace Instances and Disks in Batches
Recreate instances and their associated disks in batches of 2:
```bash
./scripts/rolling_restart.py -m module.ci_v6e_1 -b 2 -d --dir cloud-ullm-inference-ci-cd
```

### 5. Recreate Specific Instances
Recreate only instances at indexes `0`, `2`, and `5` of `module.ci_cpu_64_core`:
```bash
./scripts/rolling_restart.py -m module.ci_cpu_64_core -i 0,2,5 --dir cloud-ullm-inference-ci-cd
```

### 6. Automated Execution with Delay
Automatically recreate all instances in `module.ci_cpu_64_core` one by one, with a 60-second delay between batches to allow the instances to boot up:
```bash
./scripts/rolling_restart.py -m module.ci_cpu_64_core --delay 60 --auto-approve --non-interactive --dir cloud-ullm-inference-ci-cd
```

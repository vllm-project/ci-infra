#!/usr/bin/env python3
"""
rolling_restart.py: Perform a rolling restart (recreation) of Terraform resources (instances, optional disks) in a target module.

Features:
  - Batching: Recreate resources in customizable batch sizes.
  - Targeting: Restricts Terraform to target only the resources being replaced to avoid applying unrelated configuration drift.
  - Selectivity: Support replacing all instances or only a specific list of comma-separated indexes.
  - Optional Disk Replacement: Recreate disks alongside instances if requested.
  - Delay: Delay between completion of a batch recreate and start of the next one.
  - Dry Run: Print commands without executing them.

Examples:
  - Dry Run:
    ./scripts/rolling_restart.py -m module.ci_v6e_1 --dir cloud-ullm-inference-ci-cd --dry-run
  - Recreate 1-by-1 (Interactive):
    ./scripts/rolling_restart.py -m module.ci_v6e_1 --dir cloud-ullm-inference-ci-cd
  - Recreate specific indexes:
    ./scripts/rolling_restart.py -m module.ci_cpu_64_core -i 0,2 --dir cloud-ullm-inference-ci-cd
"""
import argparse
import subprocess
import sys
import re
import time

def run_command(cmd):
    """Runs a shell command and returns stdout, stderr, and return code."""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return result.stdout, result.stderr, result.returncode
    except Exception as e:
        return "", str(e), -1

def get_terraform_resources(directory):
    """Runs terraform state list and returns a list of resources."""
    print(f"Fetching Terraform state list (chdir={directory})...")
    stdout, stderr, code = run_command(f"terraform -chdir={directory} state list")
    if code != 0:
        print(f"Error running terraform state list: {stderr}", file=sys.stderr)
        sys.exit(1)
    return [line.strip() for line in stdout.splitlines() if line.strip()]

def parse_resources(resources, target_module):
    """Parses resources and filters by the target module."""
    # Pattern to match: module.<module_name>.<resource_type>.<resource_name>[<index>]
    # or module.<module_name>.<resource_type>.<resource_name>["key"]
    # We want to extract resource type, name, and index/key
    pattern = re.compile(rf"^{re.escape(target_module)}\.([^.]+)\.([^.\[]+)(?:\[([^\]]+)\])?$")
    
    parsed = []
    for res in resources:
        match = pattern.match(res)
        if match:
            res_type, res_name, index = match.groups()
            parsed.append({
                "address": res,
                "type": res_type,
                "name": res_name,
                "index": index
            })
    return parsed

def parse_indexes_arg(arg_str):
    """Parses a comma-separated list of indexes (e.g. '0,2,5')."""
    if not arg_str:
        return None
    return {part.strip() for part in arg_str.split(',') if part.strip()}

def main():
    parser = argparse.ArgumentParser(description="Rolling restart/replace of Terraform resources.")
    parser.add_argument("-m", "--module", required=True, help="Terraform module to target (e.g., module.ci_v6e_1)")
    parser.add_argument("-b", "--batch-size", type=int, default=1, help="Number of instances to replace per batch (default: 1)")
    parser.add_argument("-d", "--replace-disks", action="store_true", help="Also replace associated disks (google_compute_disk)")
    parser.add_argument("--auto-approve", action="store_true", help="Pass -auto-approve to terraform apply")
    parser.add_argument("--delay", type=int, default=0, help="Delay in seconds between the completion of a batch recreate and the start of the next recreate (default: 0)")
    parser.add_argument("--non-interactive", action="store_true", help="Do not prompt for confirmation before each batch")
    parser.add_argument("--dir", default=".", help="Directory containing Terraform configuration (default: .)")
    parser.add_argument("--dry-run", action="store_true", help="Print the commands that would be executed without running them")
    parser.add_argument("-i", "--indexes", help="Comma-separated list of specific instance indexes to replace (e.g. 0,2,5)")
    
    args = parser.parse_args()
    
    # Parse indexes argument early
    target_indexes = parse_indexes_arg(args.indexes) if args.indexes else None
            
    # 1. Fetch all resources
    resources = get_terraform_resources(args.dir)
    
    # 2. Filter by targeted module
    module_resources = parse_resources(resources, args.module)
    if not module_resources:
        print(f"No resources found for module '{args.module}' in directory '{args.dir}'.")
        # Print a list of unique modules found in state to help user
        modules = set()
        for res in resources:
            parts = res.split('.')
            if len(parts) > 1 and parts[0] == 'module':
                modules.add(f"module.{parts[1]}")
        if modules:
            print("Available modules in state:")
            for m in sorted(modules):
                print(f"  - {m}")
        sys.exit(1)
        
    # 3. Categorize resources into instances and disks
    instances = []
    disks = []
    
    instance_types = {"google_tpu_v2_vm", "google_compute_instance"}
    disk_types = {"google_compute_disk"}
    
    for res in module_resources:
        if res["type"] in instance_types:
            instances.append(res)
        elif res["type"] in disk_types:
            disks.append(res)
            
    if not instances:
        print(f"No instance resources (google_tpu_v2_vm or google_compute_instance) found in module '{args.module}'.")
        sys.exit(1)
        
    # Group by index
    # We want to map: index -> { 'instance': resource_address, 'disk': resource_address }
    grouped = {}
    for inst in instances:
        idx = inst["index"] or "0" # Default to "0" if no index
        if idx not in grouped:
            grouped[idx] = {}
        grouped[idx]["instance"] = inst["address"]
        
    for disk in disks:
        idx = disk["index"] or "0"
        if idx in grouped:
            grouped[idx]["disk"] = disk["address"]
        else:
            # If there's a disk but no corresponding instance listed in state (unlikely but possible)
            grouped[idx] = {"disk": disk["address"]}
            
    # Sort indexes (try numeric sort if they are digits, otherwise string sort)
    def get_sort_key(item):
        key = item[0]
        if key.isdigit():
            return (0, int(key))
        return (1, key)
        
    sorted_groups = sorted(grouped.items(), key=get_sort_key)
    
    if target_indexes is not None:
        sorted_groups = [g for g in sorted_groups if g[0] in target_indexes]
        if not sorted_groups:
            print(f"None of the specified indexes ({args.indexes}) were found in the state for module '{args.module}'.")
            sys.exit(1)
    
    print(f"\nFound {len(sorted_groups)} instances in module '{args.module}':")
    for idx, info in sorted_groups:
        inst_addr = info.get("instance", "None")
        disk_addr = info.get("disk", "None")
        print(f"  Index {idx}: Instance = {inst_addr}, Disk = {disk_addr}")
        
    # 4. Prepare batches
    batches = []
    current_batch = []
    for idx, info in sorted_groups:
        current_batch.append((idx, info))
        if len(current_batch) == args.batch_size:
            batches.append(current_batch)
            current_batch = []
    if current_batch:
        batches.append(current_batch)
        
    print(f"\nSplitting into {len(batches)} batches (batch size: {args.batch_size})")
    
    # 5. Process batches
    for i, batch in enumerate(batches):
        print(f"\n=== Batch {i+1}/{len(batches)} ===")
        
        replace_targets = []
        for idx, info in batch:
            if "instance" in info:
                replace_targets.append(info["instance"])
            if args.replace_disks and "disk" in info:
                replace_targets.append(info["disk"])
                
        print("Resources to replace:")
        for target in replace_targets:
            print(f"  - {target}")
            
        # Build terraform command
        tf_cmd = ["terraform", f"-chdir={args.dir}", "apply"]
        for target in replace_targets:
            tf_cmd.append(f"-replace={target}")
            tf_cmd.append(f"-target={target}")
            
        if args.auto_approve:
            tf_cmd.append("-auto-approve")
            
        if args.dry_run:
            print(f"[DRY-RUN] Would run: {' '.join(tf_cmd)}")
            continue

        # Prompt for confirmation
        if not args.non_interactive:
            confirm = input("\nProceed with this batch? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                print("Aborted by user.")
                sys.exit(0)
                
        print(f"Running: {' '.join(tf_cmd)}")
        
        # We run the command interactively so the user can see progress and input 'yes' if not auto-approved
        proc = subprocess.run(tf_cmd)
        if proc.returncode != 0:
            print(f"\nTerraform apply failed with code {proc.returncode}. Stopping rolling restart.")
            sys.exit(proc.returncode)
            
        print(f"Batch {i+1} completed successfully.")
        
        # Delay between batches
        if i < len(batches) - 1 and args.delay > 0:
            print(f"Waiting {args.delay} seconds before starting the next batch...")
            time.sleep(args.delay)
            
    print("\nRolling restart completed successfully!")

if __name__ == "__main__":
    main()

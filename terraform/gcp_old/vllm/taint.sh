#!/bin/bash
# Usage: ./taint.sh "0,2,5"

if [ -z "$1" ]; then
  echo "Usage: $0 \"index1,index2,...\""
  exit 1
fi

# Split comma-separated indexes into array
IFS=',' read -ra INDEXES <<< "$1"

for idx in "${INDEXES[@]}"; do
  echo "Tainting index $idx..."
  terraform taint "module.ci_v6.google_compute_disk.disk_east5_b[$idx]"
  terraform taint "module.ci_v6.google_tpu_v2_vm.tpu_v6_ci[$idx]"
done

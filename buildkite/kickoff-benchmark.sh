#!/usr/bin/env bash

# NOTE(simon): this script runs inside a buildkite agent with CPU only access.
set -euo pipefail

# install yq to merge yaml files
(which tar) || (apt update && apt install -y tar)
(which wget) || (apt update && apt install -y wget)

# download yq binary
rm -f yq_linux_amd64
(wget https://github.com/mikefarah/yq/releases/download/v4.44.2/yq_linux_amd64.tar.gz -O - |\
  tar xz)
vllm_root_directory=$(pwd)

# the final buildkite pipeline
rm -f final.yaml
touch final.yaml

merge () {
  # append $1 to final.yaml, and resolve anchors
  $vllm_root_directory/yq_linux_amd64 -n "load(\"final.yaml\") *+ (load(\"$1\") | explode(.))" > temp.yaml
  mv temp.yaml final.yaml
}


# If BUILDKITE_PULL_REQUEST != "false", then we check the PR labels using curl and jq
if [ "$BUILDKITE_PULL_REQUEST" != "false" ]; then
  PR_LABELS=$(curl -s "https://api.github.com/repos/vllm-project/vllm/pulls/$BUILDKITE_PULL_REQUEST" | jq -r '.labels[].name')

  # put nightly benchmark in the front, as it contains a blocking step.
  if [[ $PR_LABELS == *"nightly-benchmarks"* ]]; then
    echo "This PR has the 'nightly-benchmark' label. Proceeding with the nightly benchmarks."
    merge ".buildkite/nightly-benchmarks/nightly-pipeline.yaml"
  fi

  if [[ $PR_LABELS == *"perf-benchmarks"* ]]; then
    echo "This PR has the 'perf-benchmarks' label. Proceeding with the performance benchmarks."
    merge ".buildkite/nightly-benchmarks/benchmark-pipeline.yaml"
  fi

elif [ "$BUILDKITE_BRANCH" == "main" ]; then
  echo "This is a build from a new commit on main branch. Proceeding with the performance benchmark."
  merge ".buildkite/nightly-benchmarks/benchmark-pipeline.yaml"
else
  echo "Skipping performance benchmark."
  exit 0
fi


if [ -s final.yaml ]; then
  # final.yaml is not an empty file. Proceed with the pipeline upload.
  buildkite-agent pipeline upload final.yaml
fi


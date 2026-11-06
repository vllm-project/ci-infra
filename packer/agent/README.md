# Buildkite agent image upgrade

`install-agent.sh` installs Buildkite agent 3.138.2 into the Elastic CI Stack's
stable-agent path, verifying a pinned SHA256 checksum and the Secrets CLI.
It preserves the stack's service, hooks, boot configuration, and beta channel.
The daily CPU AMI build also runs this installer so it retains the upgrade.

For a release-only image bake, run the existing `rebuild-cpu-build-ami` pipeline
with `AGENT_ONLY=1`, `AGENT_SOURCE_AMI_AMD64`, and `AGENT_SOURCE_AMI_ARM64` set
to the current worker AMIs. This preserves the x86 Docker cache and produces
two manifest artifacts. This mode does not update SSM, launch templates or
ASGs, or delete old images. Ordinary image publication is limited to `main`.

Before rollout, boot each image and verify agent registration, checkout and a
job using step-level Buildkite Secrets. Update release ASGs to new launch
template versions only after validation. Preserve each previous launch
template version for rollback and let existing jobs finish naturally.

# Building a new GPU image (e.g. update CUDA)
Install Packer first: https://developer.hashicorp.com/packer/tutorials/docker-get-started/get-started-install-cli
Then install Amazon plugin on Packer: `packer plugins install github.com/hashicorp/amazon`

1. Maybe adjust the script in `gpu/scripts/install-nvidia-docker.sh`
2. Check GPU instance compatibility with the CUDA version
3. The instance type (the GPUs) in `gpu/buildkite-gpu-ami.pkr.hcl` should match the ones defined in `../ci-stack-module/main.tf`
4. Build using packer: `cd gpu && packer build buildkite-gpu-ami.pkr.hcl`

Note that this builds the new image on top of the latest Buildkite agent AMI (not our own - theirs!).
This means we may have to update the Buildkite CI stack version as well, as the scripts might
otherwise mismatch.

For this, go to `../ci-stack-module-variables.tf` and change `elastic_ci_stack_version` to the
latest version.

5. Change the resulting image into the GPU queues in `ci-stack-module/main.tf` (search for `ImageId`)
6. Make sure to annotate the image ID.
7. If you had any failing builds, consider removing the failed AMI or snapshots, if necessary.

Then deploy using `terraform apply`

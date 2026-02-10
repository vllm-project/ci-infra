# vLLM Continuous Integration (CI) Infrastructure

## Overview
This repository contains the infrastructure and bootstrap code for the vLLM continuous integration pipeline using Buildkite.

Current CI Infrastructure Setup:

- AWS Buildkite Elastic CI Stack: Infrastructure code in `terraform/aws`
- TPU v5/v6e Nodes on GCP: Infrastructure code in `terraform/gcp_old`
- GKE Cluster on GCP (currently not in use): Infrastructure code in `terraform/gcp`

Buildkite bootstrap scripts & pipeline template files are located in the `buildkite/` directory.

## How vLLM Uses Buildkite for CI
vLLM leverages Buildkite for CI workflow. Whenever a commit is pushed to the vLLM GitHub repository, a Buildkite webhook triggers an event that initiates a new build in the Buildkite pipeline with relevant details like Github branch and commit.

Build Process Overview:

- Bootstrap Step:
    - Executed via `buildkite/bootstrap.sh`.
    - Utilizes a CI Jinja2 template (`buildkite/test-template-ci.j2`) along with the [list of tests from vLLM](https://github.com/vllm-project/vllm/blob/main/.buildkite/test-pipeline.yaml) to render a Buildkite YAML configuration that defines all build/test steps and their configurations.
    - Uploads the rendered YAML to Buildkite to initiate the build.
    - Note: We are transitioning to a custom Buildkite pipeline generator to replace the Jinja2 template rendering soon.

- Job Queueing and Execution:
    - Each Buildkite step is associated with an agent queue.
    - After uploaded, steps are pushed into the queue, waiting to be picked up by a Buildkite agent.
    
## Buildkite Agent Cluster Setup
We use the [Buildkite Elastic CI Stack](https://github.com/buildkite/elastic-ci-stack-for-aws) to set up our autoscaling Buildkite agent cluster on AWS.

Components of the stack for each Agent Queue:

- AWS CloudFormation Stack:
    - Contains an EC2 Auto Scaling Group and an AWS Lambda function.

- EC2 Auto Scaling Group:
    - Automatically scales number of EC2 instances based on the workload from the Buildkite queue.
    - Each EC2 instance comes with a Buildkite agent that executes jobs.

- AWS Lambda Function:
    - Periodically polls Buildkite to assess capacity needs for the queue and adjusts the size of the Auto Scaling Group accordingly.

## How to test changes in this repo
1. Create a feature branch on this repo, say named `my-feature-branch`. If you can't create a feature branch, ping @khluu to add you into the repo.
2. Once the branch is created, you can start making changes and commit to the branch.
3. After the changes are pushed to the branch, wait a few minutes, then create a new build on Buildkite with this environment variable `VLLM_CI_BRANCH=my-feature-branch` to test your changes against vLLM codebase.

Please note that when creating a new build on Buildkite:
- Please do it on your own feature branch/fork branch on vLLM, preferrably a branch that is up to date with `main`.
- If it's a fork branch, `HEAD` cannot be used as commit when creating a build. You have to put in the hash of the latest commit on your branch. Also, format the branch name to include your fork prefix as `<fork/username>:<branch name on fork>`.

## How to onboard runners onto CI cluster on Buildkite
The machines communicate with Buildkite server via an agent being installed on the machine. There are multiple ways agents can be installed, depending on how the machines are set up:
1. [Buildkite Elastic CI stack](https://buildkite.com/docs/agent/v3/elastic-ci-aws/elastic-ci-stack-overview) if you want the compute to be autoscaling EC2 instances.
2. [Buildkite K8s agent stack](https://github.com/buildkite/agent-stack-k8s) if you want machines to be managed/orchestrated in a Kubernetes cluster.
3. [Buildkite agent](https://buildkite.com/docs/agent/v3) if you already have existing standalone machines.

For all of these approaches, you would need the following info to set up (please contact @khluu on #sig-ci channel - vllm-dev.slack.com to get them):
- Buildkite agent token
- Buildkite queue name
- (optional) Buildkite cluster UUID

If you go with option 1 or 2, these info would need to be provided when you setup the stack. 
For option 3, it doesn't require it when installing agent. After installation, you would need to manually:
- Add these info in the agent config (usually located in `/etc/buildkite-agent/buildkite-agent.cfg`)
- Restart the agent (usually `systemctl stop buildkite-agent` then `systemctl start buildkite-agent` works)

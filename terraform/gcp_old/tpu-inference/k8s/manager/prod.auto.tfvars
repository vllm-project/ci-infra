project_id  = "cloud-ullm-inference-ci-cd"
name_prefix = "tpu-ci"
network     = "projects/cloud-ullm-inference-ci-cd/global/networks/default"

# Saturday and Sunday 15:00-23:00 UTC (08:00-16:00 PDT, 07:00-15:00 PST): the
# fleet's quietest hours. On 10-10 and 10-11 at most 34 of the 128 v7x chips
# and none of the v6e chips were running in them; in the same hours on 10-12,
# the first weekday with every PR on kube, 12-132 were, and 40-130 run every
# night, weekends included, for the nightlies.
#
# The start and end are the first occurrence, a Saturday already past: the
# date only anchors the series, and what repeats is their time of day and the
# 8 hours between them.
maintenance_window = {
  start_time = "2026-10-10T15:00:00Z"
  end_time   = "2026-10-10T23:00:00Z"
  recurrence = "FREQ=WEEKLY;BYDAY=SA,SU"
}

# Read by both terraform and scripts/generate_manifests.py; see variables.tf for
# why there is only the one.
namespace = "buildkite"

# The token the bare-metal agents already register with, so the kube fleet joins
# the same Buildkite org as the queues it is replacing.
agent_token_secret_id = "vllm_buildkite_agent_token"

# Credentials any pipeline may ask for by name, keyed by the environment
# variable a workload reads them from. None is created here: each is created
# with gcloud in this project and shared with the bare-metal lane, so that the
# two lanes fetch a gated model under one account and report into one suite.
env_secrets = {
  # A gated model cannot be fetched without it, and the fleet's model cache is
  # shared, so the first step to want one pays for every later one.
  HF_TOKEN = {
    project = "cloud-ullm-inference-ci-cd"
    secret  = "vllm_buildkite_hf_token"
  }
  # Test Engine. The collector runs inside the workload rather than in the
  # agent, so the token has to reach the pod; without it a suite still passes
  # and reports nothing, which is the failure mode worth designing against.
  # The vllm org's, as the ci-cd bare-metal hosts use: a token names its org
  # and suite, and the tpu-commons one reports into an org being retired.
  BUILDKITE_ANALYTICS_TOKEN = {
    project = "cloud-ullm-inference-ci-cd"
    secret  = "vllm_buildkite_analytics_token"
  }
}

# The GitHub deploy key an agent pod clones a private repository with, which is
# vllm-torchtpu; tpu-inference is public and needs none.
git_ssh_key_secret_id = "vllm_torchtpu_deploy_key"

# us-central1 to sit with the rest of the CI control plane: the monitoring VM,
# the cache buckets, and the Artifact Registry these nodes pull from. The
# manager holds no TPUs, so it is not tied to a reservation's zone.
manager_region                 = "us-central1"
manager_subnetwork             = "projects/cloud-ullm-inference-ci-cd/regions/us-central1/subnetworks/default"
manager_master_ipv4_cidr_block = "172.16.0.0/28"

# What nodes may pull, manager and worker alike. tpu-inference-ci-docker is in
# asia-south1 and is not listed: nothing in this lane pulls from it, and a
# repository absent here is simply unreadable.
image_repositories = [
  # This fleet's own images - the launcher - as opposed to the CI images below,
  # which are built from the tpu-inference repo and named by a step.
  { location = "us-central1", repository = "tpu-ci" },
  { location = "us-central1", repository = "tpu-inference" },
  { location = "us-central1", repository = "tpu-inference-ci" },
  { location = "us-central1", repository = "vllm-torchtpu" },
  { location = "us-central1", repository = "vllm-torchtpu-ci" },
  { location = "us-central1", repository = "vllm-on-tpu-docker-container" },
]

# Host memory per machine type, in the decimal GB the machine family
# documentation quotes. Add an entry when a pool below names a new machine
# type; see variables.tf.
machine_memory_gb = {
  "ct6e-standard-1t"  = 176
  "ct6e-standard-4t"  = 720
  "ct6e-standard-8t"  = 1440
  "tpu7x-standard-1t" = 240
  "tpu7x-standard-4t" = 960
}

kueue_version       = "0.20.1"
jobset_version      = "0.12.0"
agent_stack_version = "0.51.0"

# A queue of its own, so this fleet and the bare-metal one run side by side and
# a pipeline moves over one step at a time.
buildkite_queue = "kube"

auth_plugin_image       = "gcr.io/google.com/cloudsdktool/google-cloud-cli:584.0.0"
auth_plugin_source_path = "/usr/lib/google-cloud-sdk/bin/gke-gcloud-auth-plugin"

# Built by kueue/launcher/cloudbuild.yaml from the same Cloud CLI image the
# auth plugin is copied out of. The suffix after the CLI version is the
# Dockerfile revision, bumped when the Dockerfile changes and the base image
# does not, so a tag names one set of bytes.
launcher_image = "us-central1-docker.pkg.dev/cloud-ullm-inference-ci-cd/tpu-ci/launcher:584.0.0-1"

# How long a workload may hold the chips by default, and the most a manifest may
# ask for instead - twelve hours, where the disagg manifests sit. The agent Job's
# own deadline is derived from these rather than set beside them, so the two
# cannot drift apart; see agent_stack_values.tpl.
#
# The default is a ceiling on a hung test rather than a target, so it sits above
# the longest honest run; Buildkite's own cap on a step bounds the wait anyway.
tpu_test_max_seconds    = 28800
tpu_runtime_max_seconds = 43200

# Half a day in line for chips, which is longer than anything currently uses.
# The multi-host glm step needs the whole cohort, so it waits out every other
# v7x workload before it can start.
tpu_queue_max_seconds = 43200

# An hour between a reservation and a pod. Not a broken-detector: it bounds the
# one state no other budget here can see, where the chips are reserved and
# nothing is using them. Warm dispatch measures nine seconds, and the rest is
# for a shape change - quota is chips but a slice is a topology, so eight free
# chips as two 2x2x1 nodes do not admit a 2x2x2 until those drain and a
# two-host slice is built in their place.
tpu_admission_max_seconds = 3600

# Two minutes reserved with no worker named, against a normal dispatch of a few
# seconds, is a dropped dispatch rather than a slow one; see variables.tf. The
# stall is permanent within a second of starting, and the resubmission waits for
# any copy a worker already holds to go, so waiting longer only holds the chips.
tpu_dispatch_retry_seconds = 120
tpu_dispatch_retries       = 2

# Long enough for a TPU node to come up from zero, short of recoveryTimeout (30
# minutes, kueue/common-config.yaml), so a replacement pod stuck on Spot is
# moved before Kueue requeues the workload, possibly onto Spot again.
tpu_spot_wait_seconds = 900
# On 10-13, 6 of the 9 Spot pods with a failed scale-up got a node within 5
# minutes of it; the other 3 waited 7 to 13.
tpu_spot_failed_scale_up_seconds = 300
tpu_spot_after_seconds           = 600
# Pre-merge (50) and integration (30) steps may use Spot after the wait, so
# Spot also absorbs the PR load that fills the reservation in working hours,
# when no scheduled runs are queued. Post-merge (100) and oncall-fix (120)
# wait for reserved chips.
tpu_spot_max_priority = 50

# Every CI image this fleet runs is built into the manager project's Artifact
# Registry, and a step names its own tag, so the project is the boundary rather
# than the repository. Trailing slash required: without it the prefix would also
# match a longer repository name.
allowed_image_repos = [
  "us-central1-docker.pkg.dev/cloud-ullm-inference-ci-cd/",
]

# Who may deploy: each acts as the tpu-ci-deployer account, which is what
# every cluster's RBAC or IAM lets in.
deployers = [
  "user:mhhua@google.com",
]

# The workers this manager dispatches to. Each is a module block in an
# environment under ../workers/, where its shapes, quotas and reservations
# are; this is only what Terraform here needs to grant it access, restated
# from there. `dispatch = false` on an entry pauses it; see README "Pause or
# remove a worker".
workers = {
  # v6e.
  "us-east5" = {
    project  = "cloud-ullm-inference-ci-cd"
    location = "us-east5"
  }
  # v7x.
  "us-central1" = {
    project  = "cloud-ullm-inference-ci-cd"
    location = "us-central1"
  }
}

# Registered in the fleet ahead of joining workers, so registration and the
# deploy account's way into the cluster can be checked on their own.
fleet_members = {
  # Inferact's v7x worker (../workers/inferact-vllm-tpu/).
  "inferact" = {
    project  = "inferact-vllm-tpu"
    location = "us-central1"
  }
}

labels = {
  environment = "production"
  workload    = "tpu-ci"
  owner       = "tpu-inference"
}


# The Buildkite cluster the kube queue is in, beside the bare-metal TPU queues.
buildkite_org        = "vllm"
buildkite_cluster_id = "0219f117-7dc6-4a04-aee2-1619736fd800"

# Built by dashboard/cloudbuild.yaml; the tag is the build revision there.
dashboard_image = "us-central1-docker.pkg.dev/cloud-ullm-inference-ci-cd/tpu-ci/queue-dashboard:30"

# The token modules/ci_monitoring's BigQuery puller already lists these
# pipelines' builds with.
dashboard_buildkite_token_secret_id = "vllm_org_buildkite_rest_api_token"

dashboard_viewers = [
  "user:mhhua@google.com",
  # The tpu-inference team, whose pipelines run on this fleet beside
  # vllm-torchtpu's. Already admins of cloud-tpu-inference-test, where the
  # tpu-inference CI lives.
  "group:cloud-tpu-inference@twosync.google.com",
]

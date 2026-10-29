variable "project_id" {
  type        = string
  description = "GCP project holding the manager cluster."
}

variable "name_prefix" {
  type        = string
  description = "Prefix for created resources."
  default     = "tpu-ci"
}

variable "network" {
  type        = string
  description = "Self link of the network the manager cluster sits on."
}

variable "manager_region" {
  type        = string
  description = "Region for the manager cluster. Regional, not zonal: the control plane is the only thing every TPU lane depends on, and a zonal one goes away during a zone's maintenance."
}

variable "manager_subnetwork" {
  type        = string
  description = "Self link of the subnetwork for manager nodes."
}

variable "manager_master_ipv4_cidr_block" {
  type        = string
  description = "RFC1918 /28 for the manager control plane. Must not overlap any worker cluster's block, because the manager peers with all of them."
}

variable "manager_bootstrap_machine_type" {
  type        = string
  description = "Machine type for the default node pool GKE insists on creating and that remove_default_node_pool then deletes. Nothing is ever scheduled on it; it is named only so that the create does not fail on whichever family is short in the region."
  default     = "n2-standard-2"
}

variable "manager_max_cpu" {
  type        = number
  description = "Ceiling on vCPUs auto-provisioning may create across the manager. Deliberately far above what the fleet can use: a launcher pod holds no chip, so what has to fit is the Buildkite controller's in-flight limit, and a ceiling that can be reached turns a busy night into steps that never start. Chips are what should bound the fleet."
  default     = 1024
}

variable "manager_max_memory_gb" {
  type        = number
  description = "Ceiling on memory auto-provisioning may create across the manager. Generous against the CPU ceiling, so that the family fallback is free to land on a memory-heavy shape when the balanced ones are short."
  default     = 4096
}

variable "labels" {
  type        = map(string)
  description = "Labels applied to every resource that takes them."
  default     = {}
}

variable "deletion_protection" {
  type    = bool
  default = false
}

variable "release_channel" {
  type    = string
  default = "REGULAR"
}

variable "maintenance_window" {
  type = object({
    start_time = string
    end_time   = string
    recurrence = string
  })
  description = <<-EOT
    When GKE may upgrade every cluster's control plane and nodes: the first
    occurrence as RFC 3339 times, repeated by an RFC 5545 RRULE. A node upgrade
    drains each node in turn, and a drained TPU node takes a running step's pod
    with it - the Job replaces the pod and the step reruns from the start.
    Without a window GKE upgrades at any hour; on 10-13 it drained the reserved
    2x2x1 pool at midday and evicted five running steps in half an hour.

    GKE requires at least 48 hours of window in any 32 days, in pieces of at
    least 4 hours.
  EOT
}

variable "enable_private_endpoint" {
  type        = bool
  description = "Whether the control plane is reachable only from inside the VPC. False while the cluster is administered from laptops and from Buildkite agents that live outside it."
  default     = false
}

variable "namespace" {
  type        = string
  description = <<-EOT
    The one namespace, on the manager and on every worker.

    The agent-stack-k8s controller, the launcher pods it creates and the
    workloads those submit all share it, because a LocalQueue is namespaced and
    a workload names its queue from inside its own namespace. It exists on the
    workers too, because MultiKueue mirrors a workload into the namespace it
    came from.

    Here rather than only in generate_manifests.py because the grants to the
    workload's service account name it by namespace, so Terraform and the
    generator have to agree on it. Required rather than defaulted for the same
    reason: a default would be a second place the name is written. Each
    worker's module block states it again, as fleet.namespace, and the generator
    refuses one that differs.
  EOT
}

variable "workers" {
  type = map(object({
    project  = string
    location = string
  }))
  description = <<-EOT
    The workers this manager dispatches to, by name.

    Each is a module block in an environment under ../workers/, a root of its
    own applied by whoever administers it, and joins the fleet by being listed
    here: this root grants its node account and its project's workloads what
    they need from project_id, and generate_manifests.py reads its shapes and
    quotas from that module block and renders its queues. Listing a worker is
    what gives its project the fleet's secrets, so it is a decision made here
    rather than by the worker existing.

    The name is the cluster's less name_prefix, and so its Fleet membership ID.
    Project and location are restated from the module block because Terraform
    cannot read another root's configuration; generate_manifests.py refuses a
    worker whose module block disagrees.
  EOT

  validation {
    condition = alltrue([
      for w in values(var.workers) : length(regexall("^[a-z0-9]+-[a-z0-9]+[0-9]$", w.location)) > 0
    ])
    error_message = "workers[*].location must be a region, as the worker's module block states it."
  }

  # Grants and the rendered tree are keyed on this pair. Two clusters in one
  # region of one project are possible in GKE, but not in this fleet.
  validation {
    condition = length(distinct([
      for w in values(var.workers) : "${w.project}/${w.location}"
    ])) == length(var.workers)
    error_message = "Two workers have the same project and location."
  }
}

variable "worker_deployers" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Principals that apply a workers/ environment in a project other than
    project_id, as IAM members (user:, group:, serviceAccount:).

    Granted gkehub.editor on project_id, because GKE creates the cluster's
    Fleet membership here, in this project's fleet, as whoever creates the
    cluster. Nothing else of this project is theirs. The role reaches every
    membership in the fleet, so list a principal only while its worker is
    being built or rebuilt.
  EOT
}

variable "image_repositories" {
  type = list(object({
    location   = string
    repository = string
  }))
  description = "Artifact Registry repositories in project_id that manager and worker nodes may pull from. Grants are per repository; a repository absent here is not readable."
  default     = []
}

variable "agent_token_secret_id" {
  type        = string
  description = <<-EOT
    Secret Manager secret holding the Buildkite agent token, in project_id.

    Not created here: it predates this fleet and the bare-metal agents read the
    same one, so Terraform reads it and grants access to it but never owns its
    value. Named rather than defaulted because the grant is scoped to this one
    secret, and a wrong default would be a grant on the wrong thing.
  EOT
}

variable "env_secrets" {
  type = map(object({
    project = string
    secret  = string
  }))
  description = <<-EOT
    Credentials the fleet supplies to a workload that forwards the name, keyed
    by the environment variable it is read from.

    Read by both Terraform and scripts/generate_manifests.py, and the one place
    the set is written down: Terraform grants the sync read on each, the
    generator turns each into a SecretSync on every worker, and the launcher's
    registry decides from the same map whether a --env name may be supplied at
    all. A secret listed here is one every pipeline on the fleet can ask for,
    so the list is short on purpose.

    Each names its own project, because none of these belong to this fleet.
    Scoping the grant to the secret rather than its project matters more than
    usual for that reason - those projects hold other people's secrets.
  EOT
}

variable "git_ssh_key_secret_id" {
  type        = string
  description = <<-EOT
    Secret Manager secret in project_id holding the SSH deploy key for the
    private repositories this fleet builds.

    Synced into the manager's namespace and read by the checkout container of
    every agent pod; a public repository ignores it. Its algorithm is part of
    the contract - see GIT_SSH_KEY_ENV in scripts/generate_manifests.py - so
    replacing it with a key of another type is a change in two places.
  EOT
}

# The ten variables below are read by scripts/generate_manifests.py, not by
# any resource here. They live in the same tfvars so that the cluster and what
# runs on it are described in one place and change in one review, and so that
# `terraform validate` type-checks them.
#
# None of them has a default, and none should: the generator reads the tfvars
# file rather than this schema, so a default would be a value Terraform sees
# and the generator does not.

variable "kueue_version" {
  type        = string
  description = "Kueue release to install, without the leading v. Its manifests are fetched from the GitHub release at deploy time; a tag is immutable, so pinning one pins the bytes."
}

variable "jobset_version" {
  type        = string
  description = "JobSet release to install, without the leading v. Kueue's jobset integration needs the CRD present, so this is installed first."
}

variable "auth_plugin_image" {
  type        = string
  description = "Image the manager's Kueue controller copies its Connect Gateway credential plugin out of. The Google CLI image is the only place Google publishes gke-gcloud-auth-plugin as a container; the binary is static, so it runs in Kueue's distroless image."
}

variable "auth_plugin_source_path" {
  type        = string
  description = "Path to the credential plugin inside auth_plugin_image."
}

variable "agent_stack_version" {
  type        = string
  description = "agent-stack-k8s chart to install, without the leading v. Rendered with `helm template` at deploy time and applied like any other manifest; a chart version is immutable, so pinning one pins the bytes."
}

variable "buildkite_queue" {
  type        = string
  description = <<-EOT
    Buildkite queue the controller claims jobs from.

    One queue for the whole fleet, not one per TPU shape. A step names a
    profile and the launcher submits the real workload to Kueue, so the shape
    is chosen inside the cluster; a new shape is a regenerated profile
    registry, not another queue and another agent to run it.
  EOT
}

variable "launcher_image" {
  type        = string
  description = <<-EOT
    Image the launcher pod runs: the Google Cloud CLI image, for kubectl,
    gcloud and gke-gcloud-auth-plugin, plus PyYAML, which it does not ship in
    a form Python 3 can import.

    Built by hand from kueue/launcher/Dockerfile - the command is in
    kueue/launcher/cloudbuild.yaml. Nothing rebuilds it on a commit; it changes
    only when the CLI version here does.
  EOT
}

variable "allowed_image_repos" {
  type        = list(string)
  description = <<-EOT
    Registry prefixes a workload image may come from.

    WORKLOAD_IMAGE is the pipeline's to set, since CI images are built per
    commit and the cluster cannot know the tag - which in a public repo means
    a pull request's to set. The launcher refuses an image that does not start
    with one of these. An empty list accepts any image, and only makes sense
    before the queue is open to fork PRs.
  EOT

  # A prefix stopping at a repository name also matches a longer one, so
  # "…/tpu-inference" would admit "…/tpu-inference-x" from anyone who can
  # create a repository in that project.
  validation {
    condition     = alltrue([for r in var.allowed_image_repos : endswith(r, "/")])
    error_message = "Every allowed_image_repos entry must end in / so it cannot match a longer repository name."
  }
}

variable "tpu_test_max_seconds" {
  type        = number
  description = "How long a TPU workload runs for when it says nothing. The launcher puts it on the submitted workload as activeDeadlineSeconds, so a hung test releases the chips rather than holding them until the Buildkite step times out. A manifest that knows better states its own, and a single step overrides both with TPU_MAX_RUNTIME_SECONDS in its env; either way bounded by tpu_runtime_max_seconds."
}

variable "tpu_queue_max_seconds" {
  type        = number
  description = <<-EOT
    How long the launcher waits for chips before giving up.

    Generous on purpose: waiting is capacity, not a fault in the step, and a
    step that has been in line since the previous evening is waiting on busy
    hardware. Failing it there loses its place in the queue as well as its
    result.

    Stated rather than derived. This used to be whatever a total budget left
    once the run was allowed for, which meant a manifest asking to serve for
    longer silently shortened how long it could queue - two unrelated things
    moving together. The pair a step actually has is how long it may wait and
    how long it may run, so those are the two numbers.
  EOT
}

variable "tpu_admission_max_seconds" {
  type        = number
  description = <<-EOT
    How long the launcher waits for a workload that already has quota to reach
    a worker. Waiting for chips is capacity and gets tpu_queue_max_seconds;
    this bounds the state after that, where chips are committed, nothing is
    running, and no other workload can use them.

    Wide, because quota is chips and a slice is a topology: eight chips free as
    two 2x2x1 nodes do not admit a 2x2x2 until those drain and a two-host slice
    is built in their place, which is node deletion, TPU provisioning and a
    cold image pull. Unbounded it reports nothing - a stuck reservation looks
    like a long queue until the Job deadline ends the step as exit_status -1.
  EOT
}

variable "tpu_dispatch_retry_seconds" {
  type        = number
  description = <<-EOT
    How long a workload may hold a reservation without MultiKueue placing it or
    naming a worker before the launcher resubmits it.

    The stuck states inside tpu_admission_max_seconds that have a fix: the
    multikueue controller reconciles a fresh reservation once, creates no remote
    copy, and never looks again; or it names a worker, the worker admits the
    copy, and recording that on the manager is rejected because the nomination
    is gone. Either way the chips sit reserved until the hour runs out. Normal
    dispatch names a worker within seconds, and a workload waiting on a slice
    rebuild keeps its worker named, so only the stalls reach this.
  EOT
}

variable "tpu_dispatch_retries" {
  type        = number
  description = "How many times the launcher resubmits a workload whose dispatch stalled (see tpu_dispatch_retry_seconds) before leaving it to tpu_admission_max_seconds."
}

variable "tpu_spot_wait_seconds" {
  type        = number
  description = <<-EOT
    How long a pod admitted onto Spot may go unscheduled before the launcher
    resubmits its workload with Spot ruled out, once per step.

    Spot quota is a count, not capacity: Kueue admits onto it whether or not
    GCE has a Spot VM to give, and a pool that cannot scale up changes nothing
    Kueue sees. Left alone, the workload holds the admission until
    waitForPodsReady requeues it, and requeued with its Spot quota free it can
    be admitted onto Spot again. The resubmission waits for the reservation,
    as a shape without a Spot pool would.
  EOT
}

variable "tpu_spot_failed_scale_up_seconds" {
  type        = number
  description = <<-EOT
    How long a pod admitted onto Spot may stay unscheduled after the cluster
    autoscaler reports a failed scale-up for it (a FailedScaleUp event, "GCE
    out of resources") before the launcher resubmits its workload with Spot
    ruled out, as tpu_spot_wait_seconds does with no such report.

    Not at once: the autoscaler sends the event to every pod a batch of new
    nodes was for when any node in the batch fails, and the pool keeps
    retrying, so many of those pods still get a node.
  EOT
}

variable "tpu_spot_after_seconds" {
  type        = number
  description = <<-EOT
    How long a step at or below tpu_spot_max_priority waits for reserved chips
    before the launcher resubmits it with Spot allowed. Every step starts with
    Spot ruled out: Kueue sends a workload to Spot the moment the reservation
    is full, so a small overflow the reservation clears within minutes landed
    on Spot, held its quota through a stockout, and left reserved chips idle
    as they freed up. Twice the reservation's p90 scale-up.
  EOT
}

variable "tpu_spot_max_priority" {
  type        = number
  description = "The highest WorkloadPriorityClass value that may use Spot; unclassed scores 0. Steps above it wait for reserved chips, and take them first as they free up."
}

variable "tpu_runtime_max_seconds" {
  type        = number
  description = <<-EOT
    The most runtime a workload may ask for, whatever its manifest says.

    tpu_test_max_seconds is what a step gets when it has no opinion; this is
    what it gets when its opinion is too large. The thing that must hold is
    that a workload does not outlast the launcher watching it, or it holds a
    reservation nothing will clean up.

    Set above tpu_test_max_seconds, not equal to it: the disagg manifests state
    their own deadlines in hours because a serving benchmark's length is a
    property of the benchmark. Setting the two equal would make three hours a
    hard cap rather than a default and clamp them.

    With tpu_queue_max_seconds this is the whole budget - a step's worst case
    is the two added together, which is what the Buildkite step timeout has to
    clear.
  EOT
}

variable "machine_memory_gb" {
  type        = map(number)
  description = <<-EOT
    Host memory per TPU machine type, in the decimal GB the accelerator-
    optimized machine family documentation quotes.

    Every machine_type a worker's tpu_node_pools names needs one here, and a
    new machine generation or variant is the moment to add it -
    generate_manifests.py refuses a shape it cannot size rather than guessing.

    Read only by generate_manifests.py, which sizes the pod's memory-backed
    volumes and its memory request from it. Here rather than in each worker's
    root because it is a fact about the machine type, the same in every
    project, and the sizes it produces have to agree across workers that share
    a queue.
  EOT
}

variable "buildkite_org" {
  type        = string
  description = "Buildkite organization slug the fleet's pipelines belong to. Read by the queue dashboard; see dashboard.tf."
}

variable "buildkite_cluster_id" {
  type        = string
  description = <<-EOT
    UUID of the Buildkite cluster the kube queue is in. The dashboard finds
    the pipelines whose kube steps it reports by it.
  EOT
}

variable "dashboard_image" {
  type        = string
  description = <<-EOT
    Image the queue dashboard runs. Built by hand from dashboard/Dockerfile -
    the command is in dashboard/cloudbuild.yaml - and bumped here when
    dashboard/app.py changes.
  EOT
}

variable "dashboard_buildkite_token_secret_id" {
  type        = string
  description = <<-EOT
    Secret Manager secret in project_id holding a Buildkite REST API token
    with read_builds and read_pipelines, which the dashboard lists jobs with.
    Not created here, for the same reason as agent_token_secret_id.
  EOT
}

variable "dashboard_viewers" {
  type        = list(string)
  description = <<-EOT
    IAM principals IAP lets through to the dashboard, as user:, group: or
    domain: members. Nothing else reaches it: the service accepts only
    requests IAP has authenticated.
  EOT
}

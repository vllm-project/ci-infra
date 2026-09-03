variable "project_id" {
  type        = string
  description = "The GCP project ID for the manager cluster"
}

variable "name_prefix" {
  type        = string
  description = "Prefix for created resources"
  default     = "tpu-ci"
}

variable "network" {
  type        = string
  description = "Network self link"
}

variable "manager_region" {
  type        = string
  description = "Region for the manager cluster"
}

variable "manager_zones" {
  type        = list(string)
  description = "Zones for the manager cluster nodes"
}

variable "manager_subnetwork" {
  type        = string
  description = "Subnetwork for the manager cluster"
}

variable "manager_master_ipv4_cidr_block" {
  type        = string
  description = "CIDR block for manager master"
}

variable "manager_system_machine_type" {
  type    = string
  default = "e2-standard-4"
}

variable "manager_system_min_nodes" {
  type    = number
  default = 3
}

variable "manager_system_max_nodes" {
  type    = number
  default = 10
}

variable "worker_clusters" {
  type        = any
  description = "Map of worker cluster definitions"
  default     = {}
}

variable "labels" {
  type        = map(string)
  description = "Labels to apply to resources"
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

variable "enable_private_endpoint" {
  type    = bool
  default = false
}

variable "enable_private_nodes" {
  type    = bool
  default = false
}

variable "kueue_version" {
  type        = string
  description = "Helm chart version for Kueue"
  default     = "0.19.0"
}

variable "jobset_version" {
  type        = string
  description = "Helm chart version for the JobSet operator. Must be identical on manager and workers; MultiKueue mirrors JobSet objects across them. The chart has no 'latest' tag, so this is required."
  default     = "0.12.0"
}

variable "buildkite_secret_project" {
  type        = string
  description = "GCP Project ID containing the Buildkite agent token secret"
  default     = "cloud-ullm-inference-ci-cd"
}

variable "buildkite_secret_id" {
  type        = string
  description = "GCP Secret Manager secret ID for Buildkite agent token"
  default     = "buildkite-tpu-ci-agent-dev-token"
}

variable "buildkite_queue" {
  type        = string
  description = "Buildkite queue name for Agent Stack"
  default     = "kube"
}

variable "buildkite_agent_stack_chart_version" {
  type        = string
  description = "Helm chart version for agent-stack-k8s. 0.47.0 is the first release containing the podless-Job cancellation fix (buildkite/agent-stack-k8s#933), so the stock controller image is sufficient from here on."
  default     = "0.47.0"
}

variable "buildkite_agent_stack_debug" {
  type        = bool
  description = "Verbose controller logging"
  default     = true
}

variable "tpu_job_max_runtime_seconds" {
  type        = number
  description = "Deadline for the agent pod agent-stack-k8s creates per Buildkite job (job-active-deadline-seconds). Must outlast the longest TPU step end to end."
  default     = 28800 # 8h
}

variable "tpu_test_max_seconds" {
  type        = number
  description = "How long a TPU test may run once it has chips, applied to the submitted workload as activeDeadlineSeconds. The only deadline that bounds a hung test, because waiting in the queue cannot consume it."
  default     = 10800 # 3h
}

variable "tpu_total_max_seconds" {
  type        = number
  description = "How long a step may take end to end: waiting for chips plus running. Every other deadline is derived from this and tpu_test_max_seconds, so these two are the only ones to set. Note that Buildkite contributes no queue time of its own - the agent pod starts almost immediately and then waits - so this whole budget is spent inside the step."
  default     = 28800 # 8h
}

variable "create_service_accounts" {
  type        = bool
  description = "Whether to create dedicated GKE node service accounts via Terraform"
  default     = true
}

variable "manager_node_service_account" {
  type        = string
  description = "Optional existing Service Account email for manager nodes (defaults to creating one or 'default')"
  default     = null
}



variable "cache_lifecycle_age_days" {
  type        = number
  default     = 30
  description = <<-EOT
    Days before a cache object is deleted.

    Longer than the four days the bare-metal bucket uses, because the two are
    not the same kind of store. There, a persistent disk on each VM is the real
    cache and GCS only distributes it, so an expired object is re-uploaded from
    a host that still has it. Here pods are ephemeral and the bucket is the only
    copy - and nothing refreshes an object's timestamp when it is read, because
    a cache hit is a read. At four days an entry used every day would still be
    deleted, and recompiling it costs far more than storing it.
  EOT
}

variable "models_lifecycle_age_days" {
  type        = number
  default     = 120
  description = <<-EOT
    Days before a cached model object is deleted.

    Longer than the compilation cache, because the two are not alike. A
    compilation entry is cheap to recreate and is invalidated by any code change
    that alters its HLO; a model is expensive to fetch, comes from a third party
    with a request quota, and does not change at all once its commit is pinned.
  EOT
}

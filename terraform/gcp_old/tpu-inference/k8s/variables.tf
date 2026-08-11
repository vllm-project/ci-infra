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
  description = "Wall-clock cap for a TPU workload, applied to the launcher Job and to the submitted workload so the two deadlines cannot drift apart."
  default     = 10800
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



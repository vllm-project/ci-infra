# Every value here is a literal in the calling module block, or a local that
# is one, because scripts/generate_manifests.py reads that block to render the
# worker's queues: the shapes and quotas are stated once, where the node pools
# are built.

variable "fleet" {
  type = object({
    # The manager's project. Every worker joins its fleet whatever project the
    # worker is in, and Kueue and the launcher reach the worker through that
    # fleet's Connect Gateway.
    project_id = string
    # Names the fleet's GKE Hub service agent. Stated rather than looked up,
    # so that applying a worker needs no read on the manager's project.
    project_number = string
    # Both are fixed by the manager: the generator derives cluster, service
    # account and bucket names from the prefix, and the workload's service
    # account lives in the namespace.
    name_prefix = string
    namespace   = string
  })
  description = "The fleet this worker joins, as manager/prod.auto.tfvars states it. generate_manifests.py refuses a worker whose values disagree with the manager's."
}

variable "name" {
  type        = string
  description = <<-EOT
    What the worker is called: the cluster is <name_prefix>-<name>, which is
    also its Fleet membership ID, and its key in the manager's workers map is
    this name. Unique across the fleet, since every worker joins one fleet
    whatever its project. Changing it on a cluster that exists rebuilds the
    cluster.
  EOT
}

variable "project" {
  type        = string
  description = "The project the cluster, its nodes' service account and its cache buckets are in."
}

variable "location" {
  type        = string
  description = "A region; the cluster pins no zones, because only a TPU node cares which zone it is in and its own node pool pins it there."

  validation {
    condition     = length(regexall("^[a-z0-9]+-[a-z0-9]+[0-9]$", var.location)) > 0
    error_message = "location must be a region, not a zone: a zone here silently gets a zonal control plane, and changing it later rebuilds the cluster."
  }
}

variable "network" {
  type = string
}

variable "subnetwork" {
  type = string
}

variable "master_ipv4_cidr_block" {
  type        = string
  description = "RFC1918 /28 for the control plane."
}

variable "system_machine_type" {
  type    = string
  default = "e2-standard-4"
  # Sized for the cluster's own components and nothing else: the CSI drivers,
  # the metrics agent, and the per-cluster half of Kueue and JobSet. That stack
  # asks for 2.3 of the four cores with everything scheduled, and us-east5 has
  # run it on this size throughout.
  #
  # A workload role that holds no chips is not what this pool is for, however
  # much it looks like the only place such a role could go. It asks for the
  # worker-cpu compute class, which builds a node against that pod's own
  # requests and removes it afterwards. Growing this pool to fit one instead
  # buys a node that is idle between runs and still too small for the next role
  # that wants more.
  #
  # e2 despite the worker-cpu class ordering it last for supply, because that
  # ordering is about eight-core nodes built on demand at burst and this is one
  # four-core node that already exists. On the shape this pool asks for, e2 is
  # the family us-east5 actually has: n2-standard-4 is exhausted in us-east5-b,
  # which is where a surge upgrade of this pool has to land.
}

variable "system_min_nodes" {
  type    = number
  default = 1
}

variable "system_max_nodes" {
  type    = number
  default = 64
  # A ceiling, not a plan: both workers have run on one node since they were
  # built, and a node that is never asked for is never billed. High because
  # this pool should not be what limits anything - what bounds the fleet is
  # chips, and what bounds burst CPU is the worker-cpu class, which states its
  # own families and sizes. A number here that can be reached turns an
  # unrelated pod into an outage, and reaching it is silent: the autoscaler
  # reports `max node group size reached` into pod events nobody is reading.
}

variable "rapid_cache_zones" {
  type    = list(string)
  default = []
  # Zones to put a Rapid Cache in, for both of this worker's buckets. Empty
  # disables it, which is the default: a cache is billed on what it holds, and
  # holding a copy of a bucket nothing reads from that zone is spending for
  # nothing.
  #
  # Where, not just whether, because the cache is zonal and only serves reads
  # from its own zone - so the zones worth naming are the ones the TPU pools sit
  # in. Deriving it from them was tempting and is wrong: a shape can exist in a
  # zone the fleet has not run in for months, and that is exactly the cache
  # nobody wants to pay for.
  #
  # It reads through: no mount changes, no staged copy, nothing to repopulate
  # when a model is added. Measured on the 480B checkpoint against
  # us-central1-c, warm, from 483s to 304s.
}

variable "tpu_node_pools" {
  # One per TPU shape this cluster can run. The node pool's name is its shape -
  # <machine type>-<topology>, e.g. ct6e-standard-8t-2x4, plus -<slice> for each
  # of a multi-host shape's slice pools, e.g. tpu7x-standard-4t-2x2x2-0 - and
  # locals.tf derives it from the two fields below rather than taking it from
  # here, so it cannot name hardware the pool does not have.
  # generate_manifests.py names the shape's Kueue queue the same way.
  type = list(object({
    # A topology does not imply a machine type: 2x4 is eight chips either as
    # one ct6e-standard-8t or as two ct6e-standard-4t, and those differ in pod
    # count, chips per pod and JobSet parallelism.
    machine_type = string
    topology     = string

    reservation_name = string
    # The reservation is zonal and the cluster pins no zones, so this is the
    # only thing keeping a node out of a zone that cannot serve it.
    zone = string

    # Scale-down floor: nodes this shape keeps once it has booted them. Those
    # chips are unavailable to the other shapes from then on, idle or not.
    min_nodes = optional(number)
    # Above this pool's share of the reservation, so the shapes compete for
    # free chips; the reservation running out is what stops a scale-up.
    # Single-host shapes only, as is min_nodes: a multi-host shape sets slices
    # instead, since GKE sizes each of its pools at exactly one slice.
    max_nodes = optional(number)

    # This shape's share of the reservation, in chips: the nominalQuota of its
    # ClusterQueue, which no resource here reads - chips the shape can always
    # have, while the queues sit in one cohort and lend out whatever is idle.
    # Summed across a cluster it should be the chips the reservation actually
    # has free, which max_nodes oversubscribes. On the manager a shape's quota
    # is the sum across every worker that has it.
    nominal_quota = number

    slices                = optional(number)
    reclaim_within_cohort = optional(string, "Never")
    # The order the shape's queue admits in, BestEffortFIFO or StrictFIFO.
    # See queue_group.yaml.tpl.
    queueing_strategy = optional(string, "BestEffortFIFO")

    # Spot capacity beside the reservation, for a single-host shape: a second
    # node pool, <shape>-spot, drawing on the region's Spot quota instead of the
    # reservation. Kueue admits a workload onto it only once the shape's
    # reserved quota, and what it can borrow from the cohort, is spent -
    # overflow, not a replacement, since the reservation is paid for whether it
    # is used or not. See "Spot overflow" in README.md.
    spot = optional(object({
      # The Spot pool's ceiling, which the Spot quota bounds rather than any
      # reservation.
      max_nodes = number
      # Chips of the shape's ClusterQueue the Spot flavor holds; the pool's
      # capacity if unset.
      nominal_quota = optional(number)
      # Zones the Spot pool may scale up in; the shape's zone if unset. No
      # reservation ties Spot to that zone, and GCE runs out of Spot VMs one
      # zone at a time. max_nodes applies in each zone; the flavor's quota is
      # what bounds the total.
      zones = optional(list(string))
    }))
  }))
  default = []

  # The pool name is derived, so a repeated shape does not collide loudly the
  # way a repeated map key would - it silently drops one of the two pools.
  validation {
    condition = length(distinct([
      for p in var.tpu_node_pools : "${p.machine_type}-${p.topology}"
    ])) == length(var.tpu_node_pools)
    error_message = "Two tpu_node_pools have the same machine type and topology. The node pool is named for that pair, so the second would overwrite the first."
  }

  validation {
    condition = alltrue([
      for p in var.tpu_node_pools : p.slices == null || (p.slices >= 1 && floor(p.slices) == p.slices)
    ])
    error_message = "tpu_node_pools[*].slices must be a whole number, at least 1."
  }

  validation {
    condition = alltrue([
      for p in var.tpu_node_pools : contains(["Never", "LowerPriority", "Any"], p.reclaim_within_cohort)
    ])
    error_message = "tpu_node_pools[*].reclaim_within_cohort must be Never, LowerPriority or Any."
  }

  validation {
    condition = alltrue([
      for p in var.tpu_node_pools : contains(["BestEffortFIFO", "StrictFIFO"], p.queueing_strategy)
    ])
    error_message = "tpu_node_pools[*].queueing_strategy must be BestEffortFIFO or StrictFIFO."
  }
}

variable "labels" {
  type        = map(string)
  description = "Labels applied to every resource here that takes them, beside managed_by and component."
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
  description = "When GKE may upgrade the cluster. Unset leaves the choice to GKE."
  default     = null
}

variable "cache_lifecycle_age_days" {
  type        = number
  default     = 30
  description = <<-EOT
    Days before a compilation cache object is deleted.

    Longer than the four days the bare-metal bucket uses, because the two are
    not alike: this bucket is per region and shared by every shape, so an entry
    is worth more and the whole namespace is only tens of gigabytes.
  EOT
}

variable "models_lifecycle_age_days" {
  type        = number
  default     = 120
  description = <<-EOT
    Days before a cached model object is deleted.

    Longer than the compilation cache, because the two are not alike. A model
    is expensive to fetch and does not change; a compilation entry is cheap to
    recreate and is invalidated by any compiler change.
  EOT
}

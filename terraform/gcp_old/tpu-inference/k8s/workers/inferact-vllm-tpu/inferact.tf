# v7x, on 4 chips of the reservation the bare-metal agents in
# ../../../inferact-vllm-tpu/ also draw on.
module "inferact" {
  source = "../../modules/worker"

  fleet = local.fleet

  name     = "inferact"
  project  = "inferact-vllm-tpu"
  location = "us-central1"

  # The network the bare-metal agents use, created in
  # ../../../inferact-vllm-tpu/network.tf: Private Google Access on the
  # subnet, and a Cloud NAT over all of it. GKE adds the pod and service
  # ranges to the subnet itself.
  network                = "projects/inferact-vllm-tpu/global/networks/vllm-ci"
  subnetwork             = "projects/inferact-vllm-tpu/regions/us-central1/subnetworks/vllm-ci-us-central1"
  master_ipv4_cidr_block = "172.16.0.0/28"

  # The two single-host v7x shapes. The 2x2x2 and 2x2x4 slices are 8 and 16
  # chips and do not fit in 4, so they run only on tpu-ci-us-central1. The
  # manager's v7x queues are one cohort over both workers, so it can still
  # lend these 4 chips to one of those shapes, which is then admitted, sent to
  # us-central1, and waits there until enough of its own chips free; that is
  # at most 4 chips' worth, and the cost of a worker too small for them.
  #
  # All 4 are nominal on 2x2x1, one slot, where most queued v7x work is;
  # 1x1x1 borrows them. Both pools can hold the whole 4, so the cohort decides
  # what runs and the pools only what a chip is shaped into.
  #
  # min_nodes is 0 throughout. The reservation is aggregate and shared with
  # Inferact's own work and the bare-metal agents, so a node held here when
  # idle is a chip neither of those can have.
  tpu_node_pools = [
    {
      machine_type     = "tpu7x-standard-1t"
      topology         = "1x1x1"
      reservation_name = "ghostfish-9mpeile911sjq"
      zone             = "us-central1-c"

      min_nodes     = 0
      max_nodes     = 4
      nominal_quota = 0
    },
    {
      machine_type     = "tpu7x-standard-4t"
      topology         = "2x2x1"
      reservation_name = "ghostfish-9mpeile911sjq"
      zone             = "us-central1-c"

      min_nodes     = 0
      max_nodes     = 1
      nominal_quota = 4
    },
  ]

  maintenance_window = local.maintenance_window
  labels             = local.labels
}

project_id               = "cloud-ullm-inference-ci-cd"
name_prefix              = "tpu-ci"
network                  = "projects/cloud-ullm-inference-ci-cd/global/networks/default"
buildkite_secret_project = "cloud-ullm-inference-ci-cd"
buildkite_queue          = "kube"

manager_region                 = "us-central1"
manager_zones                  = ["us-central1-a", "us-central1-b", "us-central1-c"]
manager_subnetwork             = "projects/cloud-ullm-inference-ci-cd/regions/us-central1/subnetworks/default"
manager_master_ipv4_cidr_block = "172.16.0.0/28"

worker_clusters = {
  southamerica-west1-a = {
    project                = "cloud-tpu-inference-test"
    location               = "southamerica-west1-a"
    node_service_account   = "32478767326-compute@developer.gserviceaccount.com"
    network                = "projects/cloud-tpu-inference-test/global/networks/default"
    subnetwork             = "projects/cloud-tpu-inference-test/regions/southamerica-west1/subnetworks/default"
    master_ipv4_cidr_block = "192.168.255.0/28"
    system_machine_type    = "e2-standard-4"
    system_min_nodes       = 1
    system_max_nodes       = 3

    tpu_pools = {
      # Two shapes on one 18-chip reservation. Nominal quota is what a lane
      # is guaranteed; max is how far it may grow by borrowing idle quota from
      # the other, reclaimed (reclaimWithinCohort: Any) when the owner needs
      # it back. Nominal sums to the reservation, so Kueue never admits more
      # than can exist. A pool with hosts > 1 is a multi-host slice pool
      # (clusters.tf); none is declared here yet.
      v6e-1-1x1 = {
        machine_type      = "ct6e-standard-1t"
        accelerator       = "v6e"
        accelerator_label = "tpu-v6e-slice"
        topology          = "1x1"
        chips_per_node    = 1
        # Ten chips of its own: with fourteen single-chip steps per suite this
        # lane is always busy, so it owns most of the reservation and keeps two
        # nodes warm. Borrowing across shapes here means tearing down one
        # shape's nodes and building the other's, about five minutes each way,
        # so nominal == max: nothing to gain from reaching into the other
        # lanes and a cold start per step to lose.
        min_nodes        = 2
        nominal_nodes    = 10
        max_nodes        = 10
        reservation_name = "cloudtpu-20250327121505-861300654"
      }
      v6e-8-2x4 = {
        machine_type      = "ct6e-standard-8t"
        accelerator       = "v6e"
        accelerator_label = "tpu-v6e-slice"
        topology          = "2x4"
        chips_per_node    = 8
        # One node. The multi-chip work is small - a handful of short steps
        # per suite that serialise onto it and reuse it - so a second node
        # would sit idle, and min_nodes 0 pays one cold start per build.
        min_nodes        = 0
        nominal_nodes    = 1
        max_nodes        = 1
        reservation_name = "cloudtpu-20250327121505-861300654"
      }
    }
  }
}

labels = {
  environment = "production"
  workload    = "tpu-ci"
  owner       = "tpu-inference"
}

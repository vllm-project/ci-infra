# The v6e worker.
module "us-east5" {
  source = "../../modules/worker"

  fleet = local.fleet

  name                   = "us-east5"
  project                = "cloud-ullm-inference-ci-cd"
  location               = "us-east5"
  network                = "projects/cloud-ullm-inference-ci-cd/global/networks/default"
  subnetwork             = "projects/cloud-ullm-inference-ci-cd/regions/us-east5/subnetworks/default"
  master_ipv4_cidr_block = "172.16.0.32/28"

  # The zone both v6e pools sit in, so the caches serve the nodes that read
  # them. A cache in any other us-east5 zone would be billed and never hit.
  rapid_cache_zones = ["us-east5-a"]

  # Reservation cloudtpu-20260828173000-731402396 in us-east5-a: 128 v6e
  # chips, 22 held by the bare-metal agents (six v6e-1, two v6e-8, for the
  # tpu-inference PRs the ramp leaves on bare), 106 here. nominal_quota
  # splits the 106 by what each shape runs (10-07..10-09: 2x4 averaged 7.3
  # chips busy and peaked at twelve slots, 1x1 averaged 1.7 with a p99 of
  # 20), so 80 on 2x4 (ten slots) and 26 on 1x1; each pool can grow to all
  # 106, so a shape borrowing the cohort's idle quota can still boot the
  # nodes for it.
  # min_nodes does partition the reservation: those chips stay with one
  # shape once booted, so that shape's queue does not lend them. The floors
  # are there for start time. The reservation is paid for whether a node
  # exists or not, v6e has the headroom (10-09 18:00 to 10-11 13:20 PT kube
  # admitted 5% of these 106 chips), and a job on a booted node starts in
  # 6-8 s (p50) against 190-215 s when it boots one. GKE boots nodes only
  # for pending pods, so a floor fills from the first bursts and is kept
  # from then on.
  tpu_node_pools = [
    {
      machine_type     = "ct6e-standard-1t"
      topology         = "1x1"
      reservation_name = "cloudtpu-20260828173000-731402396"
      zone             = "us-east5-a"

      # Eight warm. Busy nodes 10-07..10-11: p90 4, p99 20. A replay puts
      # the share of jobs that find a booted node at 81% with eight, from
      # 57% with two.
      min_nodes     = 8
      max_nodes     = 106
      nominal_quota = 26
    },
    {
      machine_type     = "ct6e-standard-8t"
      topology         = "2x4"
      reservation_name = "cloudtpu-20260828173000-731402396"
      zone             = "us-east5-a"

      # Four warm (32 chips). Busy slots 10-07..10-11: p90 2, p99 10. A
      # replay puts the share of jobs that find a booted node at 84% with
      # four, from 67% with none.
      min_nodes = 4
      # Ten slots guaranteed, and room for three more by borrowing whatever
      # the single-chip queue is not using.
      max_nodes     = 13
      nominal_quota = 80
    },
  ]

  # Our project, so the fleet's CPU-only steps may run here too.
  cpu_workloads = true

  maintenance_window = local.maintenance_window
  labels             = local.labels
}

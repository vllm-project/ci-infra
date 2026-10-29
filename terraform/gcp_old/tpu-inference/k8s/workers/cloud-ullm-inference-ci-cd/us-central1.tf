# The v7x worker, in us-central1 because that is where its reservation is.
module "us-central1" {
  source = "../../modules/worker"

  fleet = local.fleet

  name                   = "us-central1"
  project                = "cloud-ullm-inference-ci-cd"
  location               = "us-central1"
  network                = "projects/cloud-ullm-inference-ci-cd/global/networks/default"
  subnetwork             = "projects/cloud-ullm-inference-ci-cd/regions/us-central1/subnetworks/default"
  master_ipv4_cidr_block = "172.16.0.64/28"

  # The zone all three v7x pools sit in.
  rapid_cache_zones = ["us-central1-c"]

  # Four shapes over all 128 v7x chips of the reservation. Nominal quota is
  # what a shape can always get back: 32 on 2x2x4 (two slices, the Kimi-K3
  # pairs), two slices, 16, on 2x2x2 - an 8-chip step waits for a whole slice
  # while single-host steps take chips as they free, and two or more wanted a
  # slice at once 19% of the time (10-06..10-09) - and the other 80 on the
  # single-host shapes, 72 on 2x2x1 (18 slices) and 8 on 1x1x1. 2x2x1 holds
  # most of it because 83% of queued v7x work sits there (10-14..10-28) and
  # it has no Spot pool. 1x1x1 keeps 8 for post-merge steps: they rank above
  # tpu_spot_max_priority, so reserved chips are all they can use, and on
  # weekdays they hold up to 8 at once 90% of the time (10-19..10-27). PR
  # steps spill onto Spot after tpu_spot_after_seconds.
  # Every pool can grow to all 128 chips, so a shape can borrow whatever the
  # others leave idle: the cohort accounting decides how many run at once and
  # the node pools only decide what a chip can be shaped into.
  #
  # min_nodes is 0 throughout: the reservation has no slack, so a floor is
  # chips held out of it permanently rather than a warm node.
  tpu_node_pools = [
    {
      machine_type     = "tpu7x-standard-1t"
      topology         = "1x1x1"
      reservation_name = "cloudtpu-20251114223000-2002888989"
      zone             = "us-central1-c"

      min_nodes     = 0
      max_nodes     = 128
      nominal_quota = 8

      # Overflow onto Spot once the reservation and what this shape can
      # borrow are spent. The project has 256 Spot v7x chips in us-central1;
      # this is the only Spot pool and takes 64 of them. On 10-13 it ran 294
      # Spot starts against 19 stockout fallbacks, and its 32-chip quota was
      # full in the evening peak.
      #
      # Pinned to the shape's zone (us-central1-c): it is the only working
      # TPU7x zone in us-central1 (the AI zone refuses instances as
      # unavailable, and the other default zones have no TPU7x).
      spot = { max_nodes = 64 }
    },
    {
      machine_type     = "tpu7x-standard-4t"
      topology         = "2x2x1"
      reservation_name = "cloudtpu-20251114223000-2002888989"
      zone             = "us-central1-c"

      min_nodes     = 0
      max_nodes     = 32
      nominal_quota = 72

      # No Spot: 2x2x1 Spot VMs were stocked out on most busy evenings, and
      # each step offered Spot then waited 10-15 minutes for a node that
      # never came before falling back. On 10-13 that was 262 steps against
      # 173 Spot starts, and a replay put total queue time 27% higher with
      # it than without.
    },
    {
      # Eight chips as one slice across two VMs, placed from the named
      # workload policy in modules/worker. Sixteen slices is all 128 chips, as
      # many as the single-host pools can take, so the cohort decides how
      # many multi-host tests run at once; pod_defaults.yaml keeps each
      # JobSet's pods in one slice.
      machine_type     = "tpu7x-standard-4t"
      topology         = "2x2x2"
      reservation_name = "cloudtpu-20251114223000-2002888989"
      zone             = "us-central1-c"

      slices        = 16
      nominal_quota = 16

      # Waits for borrowers to finish instead of evicting them. Within its
      # own quota this queue is admitted ahead of any borrower once eight
      # chips are free, which in working hours is soon enough; evicting
      # threw away the borrowers' runs instead - in a replay of 10-12 to
      # 10-14, 153 evictions and 224 chip-hours of runs, mostly unclassed
      # tpu-inference steps on 1x1x1, against a total queue time 7% lower
      # without them. The cost is at night, when the nightlies keep the
      # cohort full: a slice waited p90 60 minutes instead of 45 in that
      # replay.
      reclaim_within_cohort = "Never"
    },
    {
      # Two slices nominal for the Kimi-K3 pairs, taken back only from
      # lower-priority borrowers. The pairs are nightlies (unclassed) and the
      # steps borrowing their chips overnight are mostly pre-merge PR steps,
      # so the pair waits for them rather than evicting them: an evicted step
      # reruns from the start, and a pull request outranks a nightly. When
      # the cohort is full the wait can run to an hour or two, since freed
      # chips go four at a time to the 2x2x1 backlog; the nightly still
      # finishes before the working day. Eight slice pools, all 128 chips,
      # so 2x2x4 can borrow whatever the cohort leaves idle for v7x-32
      # tests and bring-ups, as 2x2x2 can; an empty pool costs nothing.
      machine_type     = "tpu7x-standard-4t"
      topology         = "2x2x4"
      reservation_name = "cloudtpu-20251114223000-2002888989"
      zone             = "us-central1-c"

      slices        = 8
      nominal_quota = 32

      reclaim_within_cohort = "LowerPriority"
    },
  ]

  maintenance_window = local.maintenance_window
  labels             = local.labels
}

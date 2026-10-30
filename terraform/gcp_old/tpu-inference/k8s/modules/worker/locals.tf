locals {
  labels = merge(var.labels, {
    managed_by = "terraform"
    component  = "tpu-ci"
  })

  # The account scripts/deploy_manifests.py runs as. manager/ creates it under
  # the same name; deriving it here rather than taking it as a variable keeps
  # a worker's root from naming some other identity cluster-admin.
  deployer = "${var.fleet.name_prefix}-deployer@${var.fleet.project_id}.iam.gserviceaccount.com"

  # The account the fleet's dashboard reads as, which manager/ creates under
  # the same name. Derived too, so the reads in dashboard.tf go to that account
  # and no other.
  dashboard = "${var.fleet.name_prefix}-dashboard@${var.fleet.project_id}.iam.gserviceaccount.com"

  # Every pool on this cluster. The pool name is derived from the shape rather
  # than given in the tfvars, so it cannot describe hardware the pool does not
  # have.
  tpu_node_pools = {
    for pool in flatten([
      for pool in var.tpu_node_pools : [
        # The topology's dimensions padded to three with 1s, so the product
        # below is one expression: Terraform has no product(), and v7x
        # topologies are 3D where v6e's are 2D.
        for dims in [concat([for d in split("x", pool.topology) : parseint(d, 10)], [1, 1])] : [
          for chips_per_vm in [parseint(trimsuffix(reverse(split("-", pool.machine_type))[0], "t"), 10)] : [
            for slice in range(coalesce(pool.slices, 1)) :
            merge(pool, {
              shape = "${pool.machine_type}-${pool.topology}"
              name  = dims[0] * dims[1] * dims[2] > chips_per_vm ? "${pool.machine_type}-${pool.topology}-${slice}" : "${pool.machine_type}-${pool.topology}"

              # A slice wider than one VM is placed as a unit and needs a
              # placement policy. The machine type's suffix is chips per VM -
              # unlike the Buildkite queue names, where tpu7x-8 counts
              # TensorCores and is a four-chip tpu7x-standard-4t.
              is_multi_host = dims[0] * dims[1] * dims[2] > chips_per_vm
              hosts         = max(1, floor(dims[0] * dims[1] * dims[2] / chips_per_vm))

              min_nodes = dims[0] * dims[1] * dims[2] > chips_per_vm ? 0 : coalesce(pool.min_nodes, 0)
              max_nodes = (dims[0] * dims[1] * dims[2] > chips_per_vm
                ? floor(dims[0] * dims[1] * dims[2] / chips_per_vm)
              : pool.max_nodes)
              slices           = coalesce(pool.slices, 1)
              stated_min_nodes = pool.min_nodes
              stated_max_nodes = pool.max_nodes
              stated_slices    = pool.slices

              family   = split("-", pool.machine_type)[0]
              capacity = "reserved"
              zones    = [pool.zone]
            })
      ]]]
    ]) : pool.name => pool
  }

  # A Spot pool for each shape that asks for one: the same hardware, no
  # reservation, and a ceiling of its own. Named <shape>-spot, so it cannot
  # collide with a reserved pool, whose names end in the topology or a slice.
  spot_tpu_node_pools = {
    for pool in flatten([
      for pool in var.tpu_node_pools : [
        for dims in [concat([for d in split("x", pool.topology) : parseint(d, 10)], [1, 1])] : [
          for chips_per_vm in [parseint(trimsuffix(reverse(split("-", pool.machine_type))[0], "t"), 10)] :
          merge(pool, {
            shape            = "${pool.machine_type}-${pool.topology}"
            name             = "${pool.machine_type}-${pool.topology}-spot"
            reservation_name = null
            # Carried, not assumed false, so the precondition in main.tf can
            # refuse Spot for a multi-host shape.
            is_multi_host    = dims[0] * dims[1] * dims[2] > chips_per_vm
            hosts            = 1
            min_nodes        = 0
            max_nodes        = pool.spot.max_nodes
            slices           = 1
            stated_min_nodes = null
            stated_max_nodes = pool.spot.max_nodes
            stated_slices    = null
            family           = split("-", pool.machine_type)[0]
            capacity         = "spot"
            zones            = pool.spot.zones != null ? pool.spot.zones : [pool.zone]
          })
        ]
      ] if pool.spot != null
    ]) : pool.name => pool
  }

  # scripts/generate_manifests.py derives these same names for the
  # PersistentVolume volumeHandles, so the two must stay in step; a mismatch is
  # a volume pointing at a bucket that was never created, which
  # deploy_manifests.py checks for before applying.
  #
  # The hash covers project and region because bucket names are globally
  # unique, so two organisations running this would otherwise collide.
  workload_buckets = {
    for purpose, retention_days in {
      cache  = var.cache_lifecycle_age_days
      models = var.models_lifecycle_age_days
      } : purpose => {
      retention_days = retention_days
      name = join("-", [
        var.fleet.name_prefix,
        purpose,
        substr(sha256("${var.project}/${var.location}"), 0, 8),
      ])
    }
  }

  # A cache per bucket per named zone. Keyed off the bucket pair above rather
  # than rebuilding the names, so adding a third purpose gets a cache without
  # anything here changing; a worker that names no zones gets none.
  rapid_caches = merge([
    for purpose, bucket in local.workload_buckets : {
      for zone in var.rapid_cache_zones :
      "${purpose}/${zone}" => {
        purpose = purpose
        zone    = zone
      }
    }
  ]...)
}

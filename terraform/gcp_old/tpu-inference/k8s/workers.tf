# Worker clusters: where the TPUs are. Standard, not Autopilot, because a TPU
# node needs reservation affinity on a named reservation, the google.com/tpu
# taint and a placement policy carrying an explicit topology - none of which
# Autopilot lets through.
#
# The control plane is regional and the cluster sets no node_locations. The one
# thing with an opinion about zones is a TPU node, which must land in its
# reservation's zone, and each TPU pool pins that for itself.

# Nodes here are private, and Private Google Access only resolves Google's own
# endpoints: Artifact Registry and the GKE system images work without egress,
# but Kueue and JobSet are published on registry.k8s.io and time out. So every
# region a worker runs in needs a Cloud Router and a Cloud NAT, created outside
# this config because a NAT gateway covers a whole region and network - the
# manager's already serves us-central1, and a second one declared here for a
# worker in the same region would be refused. See the README before adding a
# region.
resource "google_container_cluster" "worker" {
  for_each = local.workers

  # Also the Fleet membership ID, and every worker joins the manager's fleet
  # whatever project it runs in - so two clusters in one region in different
  # projects would ask for one membership and the second apply would be refused.
  name     = "${var.name_prefix}-${each.value.short_name}"
  project  = each.value.project
  location = each.value.location

  network    = each.value.network
  subnetwork = each.value.subnetwork

  remove_default_node_pool = true
  initial_node_count       = 1

  deletion_protection = var.deletion_protection

  release_channel {
    channel = var.release_channel
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = each.value.master_ipv4_cidr_block
  }

  # Empty: GKE picks and creates the pod and service secondary ranges. The
  # us-east5 subnet has none, and hand-allocating them buys nothing here.
  ip_allocation_policy {}

  workload_identity_config {
    workload_pool = "${each.value.project}.svc.id.goog"
  }

  # How the cache buckets reach a pod. The driver is a GKE addon, off by
  # default and not installable by applying a manifest; without it a
  # PersistentVolume naming gcsfuse.csi.storage.gke.io just stays Pending.
  addons_config {
    gcs_fuse_csi_driver_config {
      enabled = true
    }
  }

  # A pod here is on another cluster from the launcher that submitted it, so a
  # value the launcher resolved would travel as plaintext in the podspec and be
  # copied again into the Kueue Workload. A secretKeyRef instead needs the
  # Secret to exist here, which is what these two addons let the sync do.
  #
  # Five minutes to match the manager: a rotation the clusters notice at
  # different times is a difference nobody would think to look for.
  secret_manager_config {
    enabled = true
    rotation_config {
      enabled           = true
      rotation_interval = "300s"
    }
  }

  secret_sync_config {
    enabled = true
    rotation_config {
      enabled           = true
      rotation_interval = "300s"
    }
  }

  # MultiKueue reaches workers through the Connect Gateway, which resolves a
  # Fleet membership rather than a kubeconfig. GKE creates the membership itself
  # from this block and ties its lifecycle to the cluster; a separate
  # google_gke_hub_membership collides with that one instead of adopting it.
  fleet {
    project = var.project_id
  }

  # Every TPU shape on a cluster draws on one reservation, so a node that has
  # gone idle is holding chips another shape cannot have until it is reaped.
  # Reaping promptly is what lets the shapes hand over; BALANCED would let it
  # sit.
  cluster_autoscaling {
    autoscaling_profile = "OPTIMIZE_UTILIZATION"
  }

  resource_labels = merge(local.common_labels, {
    role   = "worker"
    worker = each.value.short_name
  })

  lifecycle {
    ignore_changes = [
      # GKE turns this on by itself and reports it back, so it diffs on every
      # plan if tracked.
      monitoring_config,
    ]
  }
}

# Everything that is not a TPU: the metrics agent, CSI drivers, and the
# per-cluster half of anything MultiKueue installs. TPU nodes carry a NoSchedule
# taint, so without this pool a worker has nowhere to run system pods.
resource "google_container_node_pool" "worker_system" {
  for_each = local.workers

  name     = "system"
  project  = each.value.project
  cluster  = google_container_cluster.worker[each.key].name
  location = each.value.location

  # total_, not the per-zone min_node_count/max_node_count: in a regional
  # cluster those are multiplied by the number of zones, so a floor of 1 would
  # quietly become one node per zone.
  autoscaling {
    total_min_node_count = each.value.system_min_nodes
    total_max_node_count = each.value.system_max_nodes
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = each.value.system_machine_type
    image_type      = "COS_CONTAINERD"
    service_account = google_service_account.worker_nodes[each.key].email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_integrity_monitoring = true
      enable_secure_boot          = true
    }

    labels = {
      "tpu-ci.google.com/role"   = "system"
      "tpu-ci.google.com/worker" = each.value.short_name
    }

    metadata = {
      disable-legacy-endpoints = "true"
    }
  }
}

# How a tpu7x slice is placed. Compute Engine will not build a node pool's
# managed instance group around a compact placement policy for this machine
# type and points at a workload policy instead. HIGH_THROUGHPUT is the
# colocating one; the accelerator topology on it is what makes it a slice
# rather than a hint, and GKE reads it back out and labels the nodes with it.
#
# One policy per pool rather than one per topology, so a pool can be replaced
# without disturbing another that happens to be the same shape.
resource "google_compute_resource_policy" "tpu_slice" {
  for_each = {
    for key, pool in local.tpu_node_pools : key => pool
    if pool.is_multi_host && pool.family == "tpu7x"
  }

  project = local.workers[each.value.worker].project
  region  = local.workers[each.value.worker].location
  name    = "${each.value.name}-slice"

  workload_policy {
    type                 = "HIGH_THROUGHPUT"
    accelerator_topology = each.value.topology
  }
}

# One pool per TPU shape. min_nodes is a scale-down floor, so a shape keeps
# nodes it has already booted; GKE creates them only for a pending pod, never to
# reach the floor. max_nodes exceeds this pool's share of the reservation, so
# the shapes compete for what is free rather than each owning a fixed slice.
resource "google_container_node_pool" "worker_tpu" {
  for_each = local.tpu_node_pools

  name     = each.value.name
  project  = google_container_cluster.worker[each.value.worker].project
  cluster  = google_container_cluster.worker[each.value.worker].name
  location = google_container_cluster.worker[each.value.worker].location

  # The cluster pins no zones, so without this the pool spreads across the
  # region and lands in zones that cannot serve the reservation.
  node_locations = [each.value.zone]

  # The per-zone pair, not total_: node_locations pins the pool to one zone so
  # the two carry the same number, but only this pair is the field GKE sizes a
  # TPU slice against. Set as a total, the per-zone field arrives as zero and
  # the pool is rejected - "Maximum node count 0 is not a valid size of TPU pod
  # slice with topology 2x2x2" - which surfaces only on shapes spanning more
  # than one host, since a single-host pool has no slice to divide.
  #
  # ANY rather than the default: TPU autoscaling wants the zone with capacity,
  # and a balanced spread across zones cannot build a slice at all. Moot while
  # node_locations is one zone, and stated so that adding a second does not
  # quietly turn it on.
  autoscaling {
    min_node_count  = each.value.min_nodes
    max_node_count  = each.value.max_nodes
    location_policy = "ANY"
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = each.value.machine_type
    service_account = google_service_account.worker_nodes[each.value.worker].email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    # A test image here is tens of gigabytes and a pod reads part of it, so
    # starting before the pull finishes is most of the cold start.
    #
    # Streaming serves an image GKE has converted, and it converts each digest
    # once: the first node to want a freshly pushed image waits out the
    # conversion and every node after it mounts in seconds. So a slow first pull
    # is not streaming failing to engage, and caching layers on the node would
    # not shorten it - the digest is new every build.
    gcfs_config {
      enabled = true
    }

    # A serving workload maps far more regions than the 65530 default allows -
    # one per weight shard, per compiled executable and per KV buffer - and dies
    # partway through model load without this. Set on the node because the
    # alternative, an init container in every manifest, would need privilege and
    # so be refused by PodSecurity baseline on the workload namespace.
    linux_node_config {
      sysctls = {
        "vm.max_map_count" = "8388608"
      }
    }

    labels = {
      "tpu-ci.google.com/worker"  = each.value.short_name
      "tpu-ci.google.com/profile" = each.value.name
    }

    taint {
      key    = "google.com/tpu"
      value  = "present"
      effect = "NO_SCHEDULE"
    }

    # The reservation is specificReservationRequired, so a node without this
    # affinity does not draw from it - it asks for on-demand capacity and fails.
    reservation_affinity {
      consume_reservation_type = "SPECIFIC_RESERVATION"
      key                      = "compute.googleapis.com/reservation-name"
      values                   = [each.value.reservation_name]
    }

    metadata = {
      disable-legacy-endpoints = "true"
    }
  }

  # A slice wider than one VM needs a policy that places it as a unit; GKE then
  # creates one node per host and scales the pool atomically.
  #
  # Up to v6e, COMPACT asks GKE to derive a compact placement policy from the
  # topology. tpu7x refuses one and takes the named workload policy above
  # instead, and GKE stores no type at all in that case - so stating one is a
  # diff that would replace the pool on every apply.
  dynamic "placement_policy" {
    for_each = each.value.is_multi_host ? [1] : []
    content {
      type         = each.value.family == "tpu7x" ? "" : "COMPACT"
      policy_name  = each.value.family == "tpu7x" ? google_compute_resource_policy.tpu_slice[each.key].name : null
      tpu_topology = each.value.topology
    }
  }

  lifecycle {
    ignore_changes = [
      # GKE turns SMT off on a TPU host and reports threads_per_core back.
      # Untracked that reads as "remove advanced_machine_features", which is
      # ForceNew - a plan that rebuilds the pool and drops its chips.
      node_config[0].advanced_machine_features,
      node_config[0].guest_accelerator,
      node_config[0].kubelet_config,
      node_config[0].shielded_instance_config,
      upgrade_settings,
    ]
  }
}

# Only for a worker in another project. When the worker shares the manager's
# project, enabling the GKE Hub API already grants the service agent this role
# on it; declaring it here would make Terraform the owner of a binding Google
# manages, and destroy would revoke it.
resource "google_project_iam_member" "gkehub_service_agent" {
  for_each = toset([
    for w in var.worker_clusters : w.project if w.project != var.project_id
  ])

  project = each.value
  role    = "roles/gkehub.serviceAgent"
  member  = "serviceAccount:service-${data.google_project.manager.number}@gcp-sa-gkehub.iam.gserviceaccount.com"
}

data "google_project" "manager" {
  project_id = var.project_id
}

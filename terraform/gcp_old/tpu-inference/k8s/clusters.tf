# Cloud NAT for Manager Cluster (outbound image pulling)
resource "google_compute_router" "manager" {
  name    = "${var.name_prefix}-mgr-router"
  project = var.project_id
  region  = var.manager_region
  network = var.network
}

resource "google_compute_router_nat" "manager" {
  name                               = "${var.name_prefix}-mgr-nat"
  project                            = var.project_id
  region                             = var.manager_region
  router                             = google_compute_router.manager.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

# Cloud NAT for Worker Clusters (outbound image pulling)
resource "google_compute_router" "worker" {
  for_each = var.worker_clusters

  name    = "${var.name_prefix}-wkr-router-${each.key}"
  project = each.value.project
  region  = join("-", slice(split("-", each.value.location), 0, 2))
  network = each.value.network
}

resource "google_compute_router_nat" "worker" {
  for_each = var.worker_clusters

  name                               = "${var.name_prefix}-wkr-nat-${each.key}"
  project                            = each.value.project
  region                             = join("-", slice(split("-", each.value.location), 0, 2))
  router                             = google_compute_router.worker[each.key].name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

resource "google_container_cluster" "manager" {
  project  = var.project_id
  name     = "${var.name_prefix}-manager"
  location = var.manager_region

  network    = var.network
  subnetwork = var.manager_subnetwork

  deletion_protection         = var.deletion_protection
  remove_default_node_pool    = true
  initial_node_count          = 1
  node_locations              = var.manager_zones
  networking_mode             = "VPC_NATIVE"
  enable_shielded_nodes       = true
  enable_intranode_visibility = true

  release_channel {
    channel = var.release_channel
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  fleet {
    project = var.project_id
  }

  # These labels ask GKE Fleet to generate ClusterProfile inventory objects in
  # kueue-system. MultiKueue can then use federated credentials instead of a
  # stored worker kubeconfig and bearer token.
  resource_labels = merge(local.common_labels, {
    fleet-clusterinventory-management-cluster = "true"
    fleet-clusterinventory-namespace          = "kueue-system"
    role                                      = "manager"
  })

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = var.enable_private_endpoint
    master_ipv4_cidr_block  = var.manager_master_ipv4_cidr_block

    master_global_access_config {
      enabled = true
    }
  }

  addons_config {
    gce_persistent_disk_csi_driver_config {
      enabled = true
    }
  }

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

  security_posture_config {
    mode               = "BASIC"
    vulnerability_mode = "VULNERABILITY_BASIC"
  }

  monitoring_config {
    enable_components = [
      "APISERVER",
      "CONTROLLER_MANAGER",
      "DAEMONSET",
      "DEPLOYMENT",
      "HPA",
      "KUBELET",
      "POD",
      "SCHEDULER",
      "STATEFULSET",
      "STORAGE",
      "SYSTEM_COMPONENTS",
    ]
    managed_prometheus {
      enabled = true
    }
  }

  lifecycle {
    ignore_changes = [monitoring_config]
  }
}

resource "google_container_node_pool" "manager_system" {
  project  = var.project_id
  name     = "system"
  location = google_container_cluster.manager.location
  cluster  = google_container_cluster.manager.name

  # Regional node-pool initial counts are per zone. Start with one in each
  # configured zone, then let total autoscaling enforce the aggregate floor.
  initial_node_count = 1

  autoscaling {
    total_min_node_count = var.manager_system_min_nodes
    total_max_node_count = var.manager_system_max_nodes
    location_policy      = "BALANCED"
  }

  node_config {
    machine_type = var.manager_system_machine_type
    image_type   = "COS_CONTAINERD"
    service_account = coalesce(
      var.manager_node_service_account,
      try(google_service_account.manager_nodes[0].email, null),
      "default"
    )
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = {
      "tpu-ci.google.com/role" = "system"
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_integrity_monitoring = true
      enable_secure_boot          = true
    }
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }

  lifecycle {
    ignore_changes = [initial_node_count]

    precondition {
      condition     = var.manager_system_min_nodes >= length(var.manager_zones)
      error_message = "manager_system_min_nodes must provide at least one system node per manager zone."
    }
  }
}

resource "google_container_cluster" "worker" {
  for_each = var.worker_clusters

  name     = "${var.name_prefix}-${each.key}"
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
    enable_private_nodes    = var.enable_private_nodes
    enable_private_endpoint = false
    master_ipv4_cidr_block  = each.value.master_ipv4_cidr_block
  }

  ip_allocation_policy {}

  workload_identity_config {
    workload_pool = "${each.value.project}.svc.id.goog"
  }

  # Cloud Storage FUSE, so a workload can mount a bucket as a filesystem. Not
  # enabled by default in GKE, unlike the Persistent Disk driver, and it
  # requires Workload Identity - configured above.
  #
  # Nothing mounts a bucket yet, and enabling the driver changes nothing on its
  # own: a pod has to opt in with the gke-gcsfuse/volumes: "true" annotation
  # before the sidecar is injected. This is here so the option can be measured
  # against the two we already have - a cloned disk, and gs:// read directly,
  # which a workload pod can already do.
  addons_config {
    gcs_fuse_csi_driver_config {
      enabled = true
    }
  }

  # OPTIMIZE_UTILIZATION, deliberately, while the two TPU shapes share one
  # cohort. The pools are physically separate - 1-chip and 8-chip nodes drawing
  # on the same reservation - so the single-chip lane can only use the eight
  # chips an idle 8-chip node is holding once that node is torn down. Reaping
  # promptly is what makes the handover possible at all; BALANCED would let the
  # idle node sit and starve the other shape for longer.
  #
  # This stops being the right trade once each shape has its own nominal
  # capacity and no longer needs to borrow across shapes. Revisit with the pool
  # sizes, not on its own.
  cluster_autoscaling {
    autoscaling_profile = "OPTIMIZE_UTILIZATION"
  }

  resource_labels = merge(local.common_labels, {
    role   = "worker"
    worker = each.key
  })

  lifecycle {
    ignore_changes = [
      private_cluster_config,
      secret_sync_config,
      secret_manager_config,
      resource_labels["asmv2"],
      resource_labels["mesh_id"],
      monitoring_config
    ]
  }
}

resource "google_container_node_pool" "worker_system" {
  for_each = var.worker_clusters

  name       = "system"
  project    = each.value.project
  cluster    = "${var.name_prefix}-${each.key}"
  location   = each.value.location
  node_count = try(each.value.system_min_nodes, 1)

  autoscaling {
    min_node_count = try(each.value.system_min_nodes, 1)
    max_node_count = try(each.value.system_max_nodes, 3)
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type = try(each.value.system_machine_type, "e2-standard-4")
    service_account = coalesce(
      try(each.value.node_service_account, null),
      try(google_service_account.worker_nodes[each.key].email, null),
      "default"
    )
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]

    labels = merge(local.common_labels, {
      profile                    = "system"
      worker                     = each.key
      "tpu-ci.google.com/worker" = each.key
    })

    resource_labels = merge(local.common_labels, {
      profile = "system"
      worker  = each.key
    })

    metadata = {
      disable-legacy-endpoints = "true"
    }
  }

  lifecycle {
    ignore_changes = [
      node_config[0].resource_labels["asmv2"],
      node_config[0].resource_labels["mesh_id"]
    ]
  }
}

resource "google_container_node_pool" "worker_tpu" {
  for_each = local.tpu_node_pools

  name     = each.value.profile_name
  project  = each.value.project
  cluster  = "${var.name_prefix}-${each.value.worker_name}"
  location = each.value.location

  initial_node_count = each.value.min_nodes

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
    machine_type = each.value.machine_type
    service_account = coalesce(
      try(each.value.node_service_account, null),
      try(google_service_account.worker_nodes[each.value.worker_name].email, null),
      "default"
    )
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]

    gcfs_config {
      enabled = true
    }

    labels = merge(local.common_labels, {
      profile                             = each.value.profile_name
      worker                              = each.value.worker_name
      "tpu-ci.google.com/worker"          = each.value.worker_name
      "tpu-ci.google.com/profile"         = each.value.profile_name
      "cloud.google.com/gke-tpu-topology" = try(each.value.topology, "")
      "google.com/tpu-topology"           = try(each.value.kueue_topology, try(each.value.topology, ""))
      "google.com/tpu-chips-per-node"     = tostring(each.value.chips_per_node)
    })

    taint {
      key    = "google.com/tpu"
      value  = "present"
      effect = "NO_SCHEDULE"
    }

    dynamic "reservation_affinity" {
      for_each = try(each.value.reservation_name, "") != "" ? [1] : []
      content {
        consume_reservation_type = "SPECIFIC_RESERVATION"
        key                      = "compute.googleapis.com/reservation-name"
        values                   = [each.value.reservation_name]
      }
    }

    resource_labels = merge(local.common_labels, {
      profile = each.value.profile_name
      worker  = each.value.worker_name
    })

    metadata = {
      disable-legacy-endpoints = "true"
    }


  }

  # A multi-host TPU slice pool: GKE needs the topology on a placement policy
  # and creates one node per host. The pool is the atomic unit - the
  # autoscaler goes from zero to every host of the slice and back, never to a
  # part of one - which is why min/max are validated below as whole slices.
  dynamic "placement_policy" {
    for_each = each.value.is_multi_host && try(each.value.topology, null) != null ? [1] : []
    content {
      type         = "COMPACT"
      tpu_topology = each.value.topology
    }
  }

  lifecycle {
    precondition {
      condition = !each.value.is_multi_host || (
        each.value.min_nodes % each.value.hosts == 0 && each.value.max_nodes % each.value.hosts == 0
      )
      error_message = "${each.key}: a multi-host pool scales in whole slices; min_nodes and max_nodes must be multiples of hosts (${each.value.hosts})."
    }
    precondition {
      condition     = !each.value.is_multi_host || each.value.max_nodes == each.value.hosts
      error_message = "${each.key}: one GKE multi-host node pool is one slice, so max_nodes must equal hosts (${each.value.hosts}); more slices of a shape are more pools."
    }
    ignore_changes = [
      # A create-time field only: GKE reports whatever the pool has scaled to
      # since, so it drifts on its own and no apply can set it. Left tracked,
      # any edit to min_nodes reads as a change to it and forces the pool to be
      # destroyed and rebuilt, when all that was wanted is a new autoscaling
      # floor. The manager's system pool ignores it for the same reason.
      initial_node_count,
      node_config[0].guest_accelerator,
      node_config[0].reservation_affinity,
      node_config[0].kubelet_config,
      node_config[0].shielded_instance_config,
      node_config[0].windows_node_config,
      node_config[0].advanced_machine_features,
      node_config[0].resource_labels["asmv2"],
      node_config[0].resource_labels["mesh_id"],
      upgrade_settings,
      node_drain_config
    ]
  }
}

resource "google_gke_hub_membership" "worker" {
  for_each = var.worker_clusters

  project       = var.project_id
  membership_id = "${var.name_prefix}-${each.key}"
  endpoint {
    gke_cluster {
      resource_link = "//container.googleapis.com/projects/${each.value.project}/locations/${each.value.location}/clusters/${google_container_cluster.worker[each.key].name}"
    }
  }

  lifecycle {
    ignore_changes = [
      authority
    ]
  }
}

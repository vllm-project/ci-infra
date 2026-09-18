# The manager cluster: the one place that is always up. It holds the Buildkite
# controller and the Kueue control plane, and it must hold no accelerators, so
# that nothing running here competes with a test for chips. Worker clusters hold
# the TPUs and attach to this one through Fleet.
#
# Standard, like the workers, because this is where every workload podspec is
# born. Autopilot admission-mutates a podspec it considers underspecified - it
# fills in a nodeAffinity on cloud.google.com/extended-duration-pods for any pod
# that arrives without one - and MultiKueue copies the podspec to a worker
# verbatim, where no node carries that label and the pod is unschedulable with
# nothing reporting an error. A launcher that builds podspecs for another
# cluster cannot share a cluster with something that rewrites them.

# Nodes here are private, so egress - image pulls, Buildkite's API, Secret
# Manager - goes through the Cloud NAT covering this region. It is created
# outside this config, with the rest of the networking; see workers.tf.
resource "google_container_cluster" "manager" {
  project  = var.project_id
  name     = "${var.name_prefix}-manager"
  location = var.manager_region

  network    = var.network
  subnetwork = var.manager_subnetwork

  remove_default_node_pool = true
  initial_node_count       = 1

  # Only ever the pool that remove_default_node_pool deletes again, and it is
  # named because the default is e2-medium in every zone of the region at once:
  # four nodes created to be thrown away, and a shortage in any one of them
  # fails the whole cluster create. us-central1 runs out of e2 regularly.
  #
  # Read at create and never again - see the lifecycle block below.
  node_config {
    machine_type = var.manager_bootstrap_machine_type
  }

  deletion_protection = var.deletion_protection

  release_channel {
    channel = var.release_channel
  }

  fleet {
    project = var.project_id
  }

  # The fleet-clusterinventory-* labels make GKE Fleet generate ClusterProfile
  # objects in kueue-system, which is what lets MultiKueue authenticate to
  # workers with federated credentials instead of a stored kubeconfig and bearer
  # token. Nothing reads them until Kueue is installed; they are set now because
  # they have to be present when the fleet is registered.
  resource_labels = merge(local.common_labels, {
    fleet-clusterinventory-management-cluster = "true"
    fleet-clusterinventory-namespace          = "kueue-system"
    role                                      = "manager"
  })

  # Empty: GKE picks and creates the pod and service secondary ranges, as on the
  # workers.
  ip_allocation_policy {}

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = var.enable_private_endpoint
    master_ipv4_cidr_block  = var.manager_master_ipv4_cidr_block

    # Worker clusters are in other regions and all of them reach this control
    # plane.
    master_global_access_config {
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

  # There are no fixed node pools here. Every node this cluster runs is created
  # by GKE for a pod that is already pending, from whichever machine family and
  # zone has capacity at that moment - see the ComputeClass in
  # kueue/templates/compute_class.yaml.tpl for the order it tries them in. A
  # pool pinned to one machine type in one region is a stockout away from the
  # fleet having no control plane, and nothing here holds accelerators or state.
  #
  # The limits are the fleet's ceiling, not a pool's: a launcher pod waiting on
  # quota occupies a node without holding a chip, so what has to fit is the
  # Buildkite controller's in-flight limit rather than any TPU count.
  cluster_autoscaling {
    enabled = true

    resource_limits {
      resource_type = "cpu"
      maximum       = var.manager_max_cpu
    }

    resource_limits {
      resource_type = "memory"
      maximum       = var.manager_max_memory_gb
    }

    auto_provisioning_defaults {
      # Off the project default compute account, which holds tpu.admin,
      # storage.admin and project-wide secretmanager.secretAccessor because the
      # bare-metal agent VMs share it.
      service_account = google_service_account.manager_nodes.email
      oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

      disk_type  = "pd-balanced"
      disk_size  = 100
      image_type = "COS_CONTAINERD"

      management {
        auto_repair  = true
        auto_upgrade = true
      }

      shielded_instance_config {
        enable_integrity_monitoring = true
        enable_secure_boot          = true
      }
    }
  }

  lifecycle {
    ignore_changes = [
      # The default pool this describes is deleted seconds after it is created,
      # so an apply that tried to reconcile it fails with "Node pool
      # default-pool not found on update".
      node_config,

      # GKE turns these on by itself and reports them back, so they diff on
      # every plan if tracked.
      monitoring_config,
    ]
  }
}

# The workers' resources, which this root built before each worker had a root
# of its own. destroy = false: the plan forgets them here rather than deleting
# them. scripts/split_worker_state.py carries them into each worker's state.
# Either side can go first - in between, a resource is in both states or in
# neither, and neither plan deletes it.
#
# Safe to delete once applied.

removed {
  from = google_container_cluster.worker

  lifecycle {
    destroy = false
  }
}

removed {
  from = google_container_node_pool.worker_system

  lifecycle {
    destroy = false
  }
}

removed {
  from = google_container_node_pool.worker_tpu

  lifecycle {
    destroy = false
  }
}

removed {
  from = google_compute_resource_policy.tpu_slice

  lifecycle {
    destroy = false
  }
}

removed {
  from = google_service_account.worker_nodes

  lifecycle {
    destroy = false
  }
}

removed {
  from = google_project_iam_member.worker_nodes

  lifecycle {
    destroy = false
  }
}

removed {
  from = google_storage_bucket.workload

  lifecycle {
    destroy = false
  }
}

removed {
  from = google_storage_bucket_iam_member.workload

  lifecycle {
    destroy = false
  }
}

removed {
  from = google_storage_anywhere_cache.workload

  lifecycle {
    destroy = false
  }
}

# JobSet operator.
#
# Required for any workload that spans more than one pod: multi-host TPU slices
# (one pod per host, gang admitted) and prefill/decode disaggregation (separate
# server pods plus a benchmark pod). A batch/v1 Job cannot express either.
#
# Installed on the manager and on every worker. MultiKueue mirrors the JobSet
# object onto the selected worker, so the CRD and the operator must exist on
# both sides at compatible versions.

resource "helm_release" "jobset_manager" {
  provider         = helm.manager
  name             = "jobset"
  repository       = "oci://registry.k8s.io/jobset/charts"
  chart            = "jobset"
  version          = var.jobset_version
  namespace        = "jobset-system"
  create_namespace = true
  wait             = true
}

resource "null_resource" "jobset_worker" {
  for_each = var.worker_clusters

  triggers = {
    version  = var.jobset_version
    endpoint = google_container_cluster.worker[each.key].endpoint
  }

  provisioner "local-exec" {
    command = <<EOT
      gcloud container clusters get-credentials ${google_container_cluster.worker[each.key].name} \
        --location ${google_container_cluster.worker[each.key].location} \
        --project ${each.value.project}

      helm upgrade --install jobset oci://registry.k8s.io/jobset/charts/jobset \
        --version ${var.jobset_version} \
        --namespace jobset-system --create-namespace \
        --wait
    EOT
  }
}

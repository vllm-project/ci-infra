resource "helm_release" "kueue_manager" {
  provider         = helm.manager
  name             = "kueue"
  repository       = "oci://registry.k8s.io/kueue/charts"
  chart            = "kueue"
  version          = var.kueue_version
  namespace        = "kueue-system"
  create_namespace = true
  wait             = false

  values = [
    yamlencode({
      serviceAccount = {
        annotations = {
          "iam.gke.io/gcp-service-account" = coalesce(var.manager_node_service_account, try(google_service_account.manager_nodes[0].email, null))
        }
      }
      managerConfig = {
        controllerManagerConfigYaml = file("${path.module}/kueue/manager-config.yaml")
      }
    })
  ]

  # The jobset.x-k8s.io/jobset integration only registers if the JobSet CRDs
  # already exist when the Kueue controller starts.
  depends_on = [helm_release.jobset_manager]
}

resource "null_resource" "kueue_worker" {
  for_each = var.worker_clusters

  triggers = {
    version  = var.kueue_version
    endpoint = google_container_cluster.worker[each.key].endpoint
    # local-exec only re-runs when a trigger changes, so the config content has
    # to be one. Without this, editing worker-config.yaml - adding the JobSet
    # integration, for instance - silently leaves every worker on the old
    # config while terraform reports no changes.
    config = filemd5("${path.module}/kueue/worker-config.yaml")
  }

  provisioner "local-exec" {
    command = <<EOT
      gcloud container clusters get-credentials ${google_container_cluster.worker[each.key].name} \
        --location ${google_container_cluster.worker[each.key].location} \
        --project ${each.value.project}

      helm upgrade --install kueue oci://registry.k8s.io/kueue/charts/kueue \
        --version ${var.kueue_version} \
        --namespace kueue-system --create-namespace \
        --set-file managerConfig.controllerManagerConfigYaml=${path.module}/kueue/worker-config.yaml

      kubectl create clusterrolebinding kueue-workload-identity-admin \
        --clusterrole=cluster-admin \
        --user="serviceAccount:${var.project_id}.svc.id.goog[kueue-system/kueue-controller-manager]" \
        --dry-run=client -o yaml | kubectl apply -f -
    EOT
  }

  # Same CRD ordering requirement as the manager.
  depends_on = [null_resource.jobset_worker]
}

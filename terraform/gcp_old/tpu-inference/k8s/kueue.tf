resource "helm_release" "kueue_manager" {
  provider         = helm.manager
  name             = "kueue"
  repository       = "oci://registry.k8s.io/kueue/charts"
  chart            = "kueue"
  version          = "0.19.0"
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
      controllerManager = {
        featureGates = [
          {
            name    = "MultiKueueClusterProfile"
            enabled = true
          }
        ]
      }
      managerConfig = {
        controllerManagerConfigYaml = file("${path.module}/kueue/manager-config.yaml")
      }
    })
  ]
}

resource "null_resource" "kueue_worker" {
  for_each = var.worker_clusters

  triggers = {
    version  = "0.19.0"
    endpoint = google_container_cluster.worker[each.key].endpoint
  }

  provisioner "local-exec" {
    command = <<EOT
      gcloud container clusters get-credentials ${google_container_cluster.worker[each.key].name} \
        --location ${google_container_cluster.worker[each.key].location} \
        --project ${each.value.project}

      helm upgrade --install kueue oci://registry.k8s.io/kueue/charts/kueue \
        --version 0.19.0 \
        --namespace kueue-system --create-namespace \
        --set-file managerConfig.controllerManagerConfigYaml=${path.module}/kueue/worker-config.yaml

      kubectl create clusterrolebinding kueue-workload-identity-admin \
        --clusterrole=cluster-admin \
        --user="serviceAccount:${var.project_id}.svc.id.goog[kueue-system/kueue-controller-manager]" \
        --dry-run=client -o yaml | kubectl apply -f -
    EOT
  }
}

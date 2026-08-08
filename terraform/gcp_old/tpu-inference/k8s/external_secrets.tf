# External Secrets Operator Helm Installation (Manager & Worker Clusters)
# Note: Kubernetes Custom Resources (ClusterSecretStore, ExternalSecret) are generated
# and applied via the manifest generator for inspection before application.

resource "null_resource" "manager_external_secrets_helm" {
  triggers = {
    endpoint = google_container_cluster.manager.endpoint
  }

  provisioner "local-exec" {
    command = <<EOT
      gcloud container clusters get-credentials ${google_container_cluster.manager.name} \
        --location ${google_container_cluster.manager.location} \
        --project ${var.project_id}
      
      helm upgrade --install external-secrets external-secrets \
        --repo https://charts.external-secrets.io \
        --namespace external-secrets --create-namespace \
        --set installCRDs=true \
        --set serviceAccount.annotations."iam\.gke\.io/gcp-service-account"="${coalesce(var.manager_node_service_account, try(google_service_account.manager_nodes[0].email, null))}"
      
      kubectl wait --for=condition=established crd/clustersecretstores.external-secrets.io --timeout=60s
      kubectl wait --for=condition=established crd/externalsecrets.external-secrets.io --timeout=60s
    EOT
  }

  depends_on = [google_container_node_pool.manager_system]
}

resource "null_resource" "worker_external_secrets_helm" {
  for_each = var.worker_clusters

  triggers = {
    endpoint = google_container_cluster.worker[each.key].endpoint
  }

  provisioner "local-exec" {
    command = <<EOT
      gcloud container clusters get-credentials ${google_container_cluster.worker[each.key].name} \
        --location ${google_container_cluster.worker[each.key].location} \
        --project ${each.value.project}
      
      helm upgrade --install external-secrets external-secrets \
        --repo https://charts.external-secrets.io \
        --namespace external-secrets --create-namespace \
        --set installCRDs=true \
        --set serviceAccount.annotations."iam\.gke\.io/gcp-service-account"="${coalesce(try(each.value.node_service_account, null), try(google_service_account.worker_nodes[each.key].email, null))}"
      
      kubectl wait --for=condition=established crd/clustersecretstores.external-secrets.io --timeout=60s
      kubectl wait --for=condition=established crd/externalsecrets.external-secrets.io --timeout=60s
    EOT
  }

  depends_on = [google_container_node_pool.worker_system]
}

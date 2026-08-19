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
      managerConfig = {
        controllerManagerConfigYaml = file("${path.module}/kueue/manager-config.yaml")
      }
    })
  ]

  # The jobset.x-k8s.io/jobset integration only registers if the JobSet CRDs
  # already exist when the Kueue controller starts.
  depends_on = [helm_release.jobset_manager]
}

// manager-config.yaml points the MultiKueue clusterProfile at
// /plugins/gcp-auth-plugin, which nothing in chart 0.19.0 can put on the
// controller pod - it exposes no initContainers or extraVolumes values - so it
// goes on as a patch afterwards.
//
// The patch belongs here rather than in deploy_manifests.sh because the
// Deployment is the chart's. Any release that re-renders it - a kueue_version
// bump, an edit to manager-config.yaml - drops the initContainer, and the
// manager silently loses the credentials it dispatches to workers with. Keyed
// on the release revision, the patch goes back on whenever that happens,
// instead of waiting for someone to remember to run the deploy script.
resource "null_resource" "kueue_manager_auth_plugin" {
  triggers = {
    helm_revision = helm_release.kueue_manager.metadata[0].revision
    patch         = filemd5("${path.module}/kueue/manager-auth-plugin-patch.yaml")
  }

  provisioner "local-exec" {
    command = <<EOT
      set -euo pipefail
      # Its own kubeconfig, so a terraform apply does not repoint the running
      # user's kubectl at the manager.
      KUBECONFIG="$(mktemp -d)/config"
      export KUBECONFIG
      trap 'rm -rf "$(dirname "$KUBECONFIG")"' EXIT

      gcloud container clusters get-credentials ${google_container_cluster.manager.name} \
        --location ${google_container_cluster.manager.location} \
        --project ${var.project_id}

      kubectl patch deployment kueue-controller-manager -n kueue-system \
        --patch-file ${path.module}/kueue/manager-auth-plugin-patch.yaml
    EOT
  }
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

      # No cluster-admin binding here any more. What the manager's controller
      # may do in this cluster is a generated manifest, kueue-multikueue-remote,
      # scoped to mirroring workloads.
    EOT
  }

  # Same CRD ordering requirement as the manager.
  depends_on = [null_resource.jobset_worker]
}

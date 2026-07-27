# Buildkite Agent Stack Helm Release (Manager Cluster Only)
# Note: Agent Stack controller is installed on manager only so worker clusters don't race to claim Buildkite jobs.

resource "kubernetes_namespace_v1" "buildkite_manager" {
  provider = kubernetes.manager

  metadata {
    name = "buildkite"
    labels = {
      "pod-security.kubernetes.io/enforce" = "baseline"
      "pod-security.kubernetes.io/audit"   = "restricted"
      "pod-security.kubernetes.io/warn"    = "restricted"
    }
  }
}

resource "helm_release" "buildkite_agent_stack" {
  for_each = local.buildkite_queues

  provider         = helm.manager
  name             = "agent-${each.key}"
  repository       = "oci://ghcr.io/buildkite/helm"
  chart            = "agent-stack-k8s"
  namespace        = kubernetes_namespace_v1.buildkite_manager.metadata[0].name
  create_namespace = false
  force_update     = true
  cleanup_on_fail  = true
  replace          = true

  values = [
    yamlencode({
      fullnameOverride = "agent-${each.key}"
      image            = "us-central1-docker.pkg.dev/cloud-ullm-inference-ci-cd/tpu-inference-ci/agent-stack-k8s:pr-933"
      agentStackSecret = "agent-stack-k8s-secret"
      config = {
        debug               = true
        queue               = each.value.queue
        pod-pending-timeout = "180m"
        pod-spec-patch = {
          containers = [
            {
              name = "agent"
            }
          ]
          nodeSelector = {
            "cloud.google.com/gke-tpu-accelerator" = each.value.accelerator_label
            "cloud.google.com/gke-tpu-topology"    = each.value.topology
          }
        }
      }
      rbac = {
        rules = [
          {
            apiGroups = ["batch"]
            resources = ["jobs"]
            verbs     = ["get", "list", "watch", "create", "update", "delete"]
          },
          {
            apiGroups = [""]
            resources = ["pods"]
            verbs     = ["get", "list", "watch", "delete"]
          },
          {
            apiGroups = [""]
            resources = ["podtemplates", "secrets"]
            verbs     = ["get"]
          },
          {
            apiGroups = [""]
            resources = ["events"]
            verbs     = ["list"]
          }
        ]
      }
    })
  ]

  depends_on = [
    kubernetes_namespace_v1.buildkite_manager,
    null_resource.manager_external_secrets_helm
  ]
}

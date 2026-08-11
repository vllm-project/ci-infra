# Buildkite Agent Stack (manager cluster only, so worker clusters never race
# to claim Buildkite jobs).
#
# One controller, one queue. Every TPU step goes through the launcher, which
# submits the real workload as a Kueue-managed object, so the Buildkite queue
# no longer has to encode a TPU shape - the profile is an argument to `launch`.
# Adding a TPU profile is then a regenerated ConfigMap rather than another Helm
# release.
#
# The agent pod is CPU-only and carries no Kueue queue label, so Kueue never
# queues or evicts it. Two properties follow:
#
#   - The agent acquires its Buildkite job in seconds rather than after TPU
#     admission and node pool scale-up, so the job is never held reserved long
#     enough for the reservation to lapse and be picked up twice.
#   - Kueue preemption evicts the workload without killing the agent, so a
#     preempted run is a pause in the step log rather than a failed build
#     needing `retry: automatic`.
#
# Placement - node selector, TPU resources, Kueue queue label - belongs to the
# submitted workload, not here.

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
  provider         = helm.manager
  name             = "agent-stack-k8s"
  repository       = "oci://ghcr.io/buildkite/helm"
  chart            = "agent-stack-k8s"
  version          = var.buildkite_agent_stack_chart_version
  namespace        = kubernetes_namespace_v1.buildkite_manager.metadata[0].name
  create_namespace = false
  force_update     = true
  cleanup_on_fail  = true
  replace          = true

  values = [
    yamlencode({
      agentStackSecret = "agent-stack-k8s-secret"

      config = {
        id    = "tpu-ci"
        queue = var.buildkite_queue
        debug = var.buildkite_agent_stack_debug

        # Applies to the launcher pod only; the workload carries its own
        # activeDeadlineSeconds. Sized to outlast queue wait plus the run,
        # because the launcher waits for both.
        job-active-deadline-seconds = var.tpu_job_max_runtime_seconds
        pod-pending-timeout         = "180m"
      }
    })
  ]

  depends_on = [
    kubernetes_namespace_v1.buildkite_manager,
    null_resource.manager_external_secrets_helm
  ]
}

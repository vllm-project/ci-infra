# Lets the fleet's deploy into this cluster. Terraform stops at the cluster;
# what runs inside it - Kueue, JobSet, the queues, the RBAC the manager
# dispatches through, the secret syncs, the cache volumes - is installed by
# scripts/deploy_manifests.py, running as the fleet's deployer account and
# reaching this cluster through Connect Gateway. The gateway passes that
# identity through to the API server, which would authorize it by IAM on
# this project if it held any. Outside the fleet's project it holds none, so
# this binding is what lets it in. cluster-admin, because the deploy installs
# CRDs, webhooks and cluster roles.
#
# In the fleet's own project the account's IAM there covers it, so there is no
# binding to own.
#
# The one Kubernetes object Terraform creates: nothing can be deployed until
# it exists, and whoever applies this root is the one party certain to hold
# that power in the cluster. The calling root points the kubernetes provider
# at this cluster from the endpoint and ca_certificate outputs.
resource "kubernetes_cluster_role_binding_v1" "deployer" {
  count = var.project == var.fleet.project_id ? 0 : 1

  metadata {
    name = "${var.fleet.name_prefix}-deployer"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = "cluster-admin"
  }

  # A Google service account reaches the API server as a user named by its
  # email.
  subject {
    api_group = "rbac.authorization.k8s.io"
    kind      = "User"
    name      = local.deployer
  }
}

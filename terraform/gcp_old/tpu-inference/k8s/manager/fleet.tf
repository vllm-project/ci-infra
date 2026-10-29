# The fleet membership of every cluster outside this project that is a
# worker or in fleet_members, which is how Kueue's ClusterProfiles, Connect
# Gateway and the deploy reach it. A worker in this project registers itself
# through its cluster's fleet block; one elsewhere is registered here, so that
# joining this fleet is done by this root and the worker's administrators need
# no role in this project (modules/worker/main.tf).
#
# Named and placed as a fleet block would: the cluster's name, in its region,
# which is what generate_manifests.py and the launcher look a worker up by. The
# worker's own apply grants the GKE Hub service agent what registering needs
# in its project (modules/worker/iam.tf), so that apply comes first.
#
# Keyed by project/location from either map, so a cluster moving from
# fleet_members to workers keeps its membership rather than replacing it.
locals {
  registered = merge(
    {
      for name, m in var.fleet_members : "${m.project}/${m.location}" => merge(m, {
        cluster_name = "${var.name_prefix}-${name}"
      })
    },
    local.workers,
  )
}

resource "google_gke_hub_membership" "workers" {
  for_each = { for key, w in local.registered : key => w if w.project != var.project_id }

  project       = var.project_id
  membership_id = each.value.cluster_name
  location      = each.value.location

  endpoint {
    gke_cluster {
      resource_link = "//container.googleapis.com/projects/${each.value.project}/locations/${each.value.location}/clusters/${each.value.cluster_name}"
    }
  }

  labels = local.common_labels
}

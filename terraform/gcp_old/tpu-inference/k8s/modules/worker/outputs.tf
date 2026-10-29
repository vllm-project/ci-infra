output "name" {
  description = "The cluster's name, which is also its Fleet membership ID and the MultiKueueCluster the manager dispatches to it by."
  value       = google_container_cluster.this.name
}

output "location" {
  value = google_container_cluster.this.location
}

output "node_service_account" {
  description = "Email of the account the worker's nodes run as. The fleet's Artifact Registry has to grant it reader before the nodes can pull a CI image."
  value       = google_service_account.nodes.email
}

output "buckets" {
  description = "The cache buckets, by purpose. generate_manifests.py derives the same names for the PersistentVolumes."
  value       = { for purpose, bucket in google_storage_bucket.workload : purpose => bucket.name }
}

# For the calling root's kubernetes provider, which creates the deployer
# binding in deploy.tf.
output "endpoint" {
  value = google_container_cluster.this.endpoint
}

output "ca_certificate" {
  value = google_container_cluster.this.master_auth[0].cluster_ca_certificate
}

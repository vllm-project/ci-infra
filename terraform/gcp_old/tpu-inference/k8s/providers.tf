provider "google" {
  project = var.project_id
}

data "google_client_config" "current" {}

provider "helm" {
  alias = "manager"
  kubernetes {
    host                   = "https://${google_container_cluster.manager.endpoint}"
    token                  = data.google_client_config.current.access_token
    cluster_ca_certificate = base64decode(google_container_cluster.manager.master_auth[0].cluster_ca_certificate)
  }
}

provider "kubernetes" {
  alias                  = "manager"
  host                   = "https://${google_container_cluster.manager.endpoint}"
  token                  = data.google_client_config.current.access_token
  cluster_ca_certificate = base64decode(google_container_cluster.manager.master_auth[0].cluster_ca_certificate)
}

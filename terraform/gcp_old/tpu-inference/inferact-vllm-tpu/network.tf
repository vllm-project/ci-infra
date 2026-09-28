# The hosts get no external IP, like every other instance in this project.
# They reach GitHub, Hugging Face and Buildkite through Cloud NAT, and Google
# APIs (Artifact Registry, GCS, BigQuery, Spanner) through Private Google
# Access.
resource "google_compute_network" "ci" {
  provider                = google-beta.us-central1-c
  name                    = "vllm-ci"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "ci" {
  provider                 = google-beta.us-central1-c
  name                     = "vllm-ci-us-central1"
  region                   = "us-central1"
  network                  = google_compute_network.ci.id
  ip_cidr_range            = "10.20.0.0/24"
  private_ip_google_access = true
}

resource "google_compute_router" "ci" {
  provider = google-beta.us-central1-c
  name     = "vllm-ci-us-central1"
  region   = "us-central1"
  network  = google_compute_network.ci.id
}

resource "google_compute_router_nat" "ci" {
  provider                           = google-beta.us-central1-c
  name                               = "vllm-ci-us-central1"
  router                             = google_compute_router.ci.name
  region                             = "us-central1"
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  # Model and dataset downloads open many parallel connections per host.
  enable_dynamic_port_allocation      = true
  enable_endpoint_independent_mapping = false
  min_ports_per_vm                    = 1024
  max_ports_per_vm                    = 16384
}

# Hosts in a slice talk to each other freely: Ray, JAX, KV transfer and the
# CI sshd on 2222.
resource "google_compute_firewall" "internal" {
  provider      = google-beta.us-central1-c
  name          = "vllm-ci-allow-internal"
  network       = google_compute_network.ci.id
  source_ranges = [google_compute_subnetwork.ci.ip_cidr_range]

  allow {
    protocol = "all"
  }
}

# Operator SSH arrives through IAP, and OS Login decides who gets in.
resource "google_compute_firewall" "iap_ssh" {
  provider      = google-beta.us-central1-c
  name          = "vllm-ci-allow-iap-ssh"
  network       = google_compute_network.ci.id
  source_ranges = ["35.235.240.0/20"]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

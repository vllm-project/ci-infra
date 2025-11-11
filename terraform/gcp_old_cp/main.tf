module "benchmark" {
  source = "./modules/benchmark"
  providers = {
    google-beta.us-east1-d = google-beta.us-east1-d
  }

  project_id = var.project_id
}
module "ci_v6" {
  source = "./modules/ci_v6"
  providers = {
    google-beta.us-east5-b = google-beta.us-east5-b
  }  
  project_id = var.project_id
}

module "ci_v5" {
  source = "./modules/ci_v5"
  providers = {
    google-beta.us-south1-a = google-beta.us-south1-a
  }

  project_id = var.project_id
}
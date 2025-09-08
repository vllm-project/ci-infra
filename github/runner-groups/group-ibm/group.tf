resource "github_actions_runner_group" "ibm_runners" {

  name = "ibm-runners"

  restricted_to_workflows    = false
  allows_public_repositories = true
  visibility                 = "selected"

  selected_repository_ids = [
    var.vllm_id,
  ]

}

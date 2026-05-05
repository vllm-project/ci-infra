resource "github_actions_runner_group" "neural_magic_runners" {

  name = "neural-magic-runners"

  restricted_to_workflows    = false
  allows_public_repositories = true
  visibility                 = "selected"

  selected_repository_ids = [
    var.compressed_tensors_id,
    var.llm-compressor_id,
    var.speculators_id,
    var.vllm_id,
  ]

}

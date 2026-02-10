locals {
  ecr_cache_lifecycle_policy = jsonencode({
    rules = [{
      rulePriority = 1
      selection = {
        tagStatus   = "any"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 14
      }
      action = {
        type = "expire"
      }
    }]
  })
}

# ECR repositories for BuildKit cache
resource "aws_ecr_repository" "vllm_ci_test_cache" {
  name     = "vllm-ci-test-cache"
  provider = aws.us_east_1
}

resource "aws_ecr_repository" "vllm_ci_postmerge_cache" {
  name     = "vllm-ci-postmerge-cache"
  provider = aws.us_east_1
}

# Lifecycle policies for cache repositories
resource "aws_ecr_lifecycle_policy" "vllm_ci_test_cache" {
  repository = aws_ecr_repository.vllm_ci_test_cache.name
  provider   = aws.us_east_1
  policy     = local.ecr_cache_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "vllm_ci_postmerge_cache" {
  repository = aws_ecr_repository.vllm_ci_postmerge_cache.name
  provider   = aws.us_east_1
  policy     = local.ecr_cache_lifecycle_policy
}

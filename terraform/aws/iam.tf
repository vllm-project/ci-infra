resource "aws_iam_policy" "premerge_ecr_public_read_access_policy" {
  name        = "premerge-ecr-public-read-access-policy"
  description = "Policy to pull images from premerge ECR"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action = [
        "ecr-public:GetAuthorizationToken",
        "ecr-public:BatchCheckLayerAvailability",
        "ecr-public:GetDownloadUrlForLayer",
        "ecr-public:GetRepositoryCatalogData",
        "ecr-public:DescribeRepositories",
        "ecr-public:DescribeImageTags",
        "ecr-public:DescribeRegistries",
        "sts:GetServiceBearerToken"
      ]
      Resource = "arn:aws:ecr-public::936637512419:repository/vllm-ci-test-repo"
    }]
  })
}

resource "aws_iam_policy" "premerge_ecr_public_write_access_policy" {
  name        = "premerge-ecr-public-write-access-policy"
  description = "Policy to push and pull images from premerge ECR"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action = [
        "ecr-public:BatchCheckLayerAvailability",
        "ecr-public:CompleteLayerUpload",
        "ecr-public:DescribeImageTags",
        "ecr-public:DescribeImages",
        "ecr-public:DescribeRegistries",
        "ecr-public:DescribeRepositories",
        "ecr-public:GetAuthorizationToken",
        "ecr-public:GetRegistryCatalogData",
        "ecr-public:GetRepositoryCatalogData",
        "ecr-public:GetRepositoryPolicy",
        "ecr-public:InitiateLayerUpload",
        "ecr-public:ListTagsForResource",
        "ecr-public:PutImage",
        "ecr-public:PutRegistryCatalogData",
        "ecr-public:TagResource",
        "ecr-public:UploadLayerPart",
        "sts:GetServiceBearerToken"
      ]
      Resource = "arn:aws:ecr-public::936637512419:repository/vllm-ci-test-repo"
    },
    {
      Effect   = "Allow"
      Action = [
        "ecr-public:GetAuthorizationToken",
        "sts:GetServiceBearerToken"
      ],
      Resource = "*"
    }]
  })
}

resource "aws_iam_policy" "premerge_ecr_cache_read_write_access_policy" {
  name        = "premerge_ecr_cache_read_write_access_policy"
  description = "Policy to read and write cache to premerge cache repo and read from postmerge cache repo"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:CompleteLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:GetAuthorizationToken",
        "sts:GetServiceBearerToken"
      ]
      Resource = [
        "arn:aws:ecr:us-east-1:936637512419:repository/vllm-ci-test-cache"
      ]
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:GetAuthorizationToken",
          "sts:GetServiceBearerToken"
        ]
        Resource = [
          "arn:aws:ecr:us-east-1:936637512419:repository/vllm-ci-postmerge-cache"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "sts:GetServiceBearerToken"
        ],
        Resource = "*"
    }]
  })
}

resource "aws_iam_policy" "postmerge_ecr_public_read_access_policy" {
  name        = "postmerge-ecr-public-read-access-policy"
  description = "Policy to pull images from postmerge ECR"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action = [
        "ecr-public:GetAuthorizationToken",
        "ecr-public:BatchCheckLayerAvailability",
        "ecr-public:GetDownloadUrlForLayer",
        "ecr-public:GetRepositoryCatalogData",
        "ecr-public:DescribeRepositories",
        "ecr-public:DescribeImageTags",
        "ecr-public:DescribeRegistries",
        "sts:GetServiceBearerToken"
      ]
      Resource = "arn:aws:ecr-public::936637512419:repository/vllm-ci-postmerge-repo"
    }]
  })
}

resource "aws_iam_policy" "postmerge_ecr_public_read_write_access_policy" {
  name        = "postmerge-ecr-public-read-write-access-policy"
  description = "Policy to push and pull images from postmerge ECR"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action = [
        "ecr-public:BatchCheckLayerAvailability",
        "ecr-public:CompleteLayerUpload",
        "ecr-public:DescribeImageTags",
        "ecr-public:DescribeImages",
        "ecr-public:DescribeRegistries",
        "ecr-public:DescribeRepositories",
        "ecr-public:GetAuthorizationToken",
        "ecr-public:GetRegistryCatalogData",
        "ecr-public:GetRepositoryCatalogData",
        "ecr-public:GetRepositoryPolicy",
        "ecr-public:InitiateLayerUpload",
        "ecr-public:ListTagsForResource",
        "ecr-public:PutImage",
        "ecr-public:PutRegistryCatalogData",
        "ecr-public:TagResource",
        "ecr-public:UploadLayerPart",
        "sts:GetServiceBearerToken"
      ]
      Resource = "arn:aws:ecr-public::936637512419:repository/vllm-ci-postmerge-repo"
    }]
  })
}

resource "aws_iam_policy" "postmerge_ecr_cache_read_write_access_policy" {
  name        = "postmerge_ecr_cache_read_write_access_policy"
  description = "Policy to read and write cache to postmerge cache repo"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:CompleteLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:GetAuthorizationToken",
        "sts:GetServiceBearerToken"
      ]
      Resource = [
        "arn:aws:ecr:us-east-1:936637512419:repository/vllm-ci-postmerge-cache"
      ]
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "sts:GetServiceBearerToken"
        ],
        Resource = "*"
    }]
  })
}

resource "aws_iam_policy" "release_ecr_public_read_write_access_policy" {
  name        = "release-ecr-public-read-write-access-policy"
  description = "Policy to push and pull images from release ECR"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action = [
        "ecr-public:BatchCheckLayerAvailability",
        "ecr-public:CompleteLayerUpload",
        "ecr-public:DescribeImageTags",
        "ecr-public:DescribeImages",
        "ecr-public:DescribeRegistries",
        "ecr-public:DescribeRepositories",
        "ecr-public:GetAuthorizationToken",
        "ecr-public:GetRegistryCatalogData",
        "ecr-public:GetRepositoryCatalogData",
        "ecr-public:GetRepositoryPolicy",
        "ecr-public:InitiateLayerUpload",
        "ecr-public:ListTagsForResource",
        "ecr-public:PutImage",
        "ecr-public:PutRegistryCatalogData",
        "ecr-public:TagResource",
        "ecr-public:UploadLayerPart",
        "sts:GetServiceBearerToken"
      ]
      Resource = "arn:aws:ecr-public::936637512419:repository/vllm-release-repo"
    }]
  })
}

resource "aws_iam_policy" "cpu_release_ecr_public_read_write_access_policy" {
  name        = "cpu-release-ecr-public-read-write-access-policy"
  description = "Policy to push and pull images from release ECR"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action = [
        "ecr-public:BatchCheckLayerAvailability",
        "ecr-public:CompleteLayerUpload",
        "ecr-public:DescribeImageTags",
        "ecr-public:DescribeImages",
        "ecr-public:DescribeRegistries",
        "ecr-public:DescribeRepositories",
        "ecr-public:GetAuthorizationToken",
        "ecr-public:GetRegistryCatalogData",
        "ecr-public:GetRepositoryCatalogData",
        "ecr-public:GetRepositoryPolicy",
        "ecr-public:InitiateLayerUpload",
        "ecr-public:ListTagsForResource",
        "ecr-public:PutImage",
        "ecr-public:PutRegistryCatalogData",
        "ecr-public:TagResource",
        "ecr-public:UploadLayerPart",
        "sts:GetServiceBearerToken"
      ]
      Resource = "arn:aws:ecr-public::936637512419:repository/vllm-cpu-release-repo"
    }]
  })
}

resource "aws_iam_policy" "bk_stack_secrets_access" {
  name = "access-to-bk-stack-secrets"

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action = ["secretsmanager:GetSecretValue"],
      Effect = "Allow",
      Resource = [
        aws_secretsmanager_secret.ci_hf_token.arn,
        aws_secretsmanager_secret.bk_analytics_token.arn
      ]
    }]
  })
}

resource "aws_iam_policy" "bk_stack_sccache_bucket_read_access" {
  name = "read-access-to-sccache-bucket"

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action = [
        "s3:Get*",
        "s3:List",
      ],
      Effect = "Allow",
      Resource = [
        "arn:aws:s3:::vllm-build-sccache/*",
        "arn:aws:s3:::vllm-build-sccache"
      ]
    }]
  })
}

resource "aws_iam_policy" "bk_stack_sccache_bucket_read_write_access" {
  name = "read-write-access-to-sccache-bucket"

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action = [
        "s3:Get*",
        "s3:List",
        "s3:PutObject"
      ],
      Effect = "Allow",
      Resource = [
        "arn:aws:s3:::vllm-build-sccache/*",
        "arn:aws:s3:::vllm-build-sccache"
      ]
    }]
  })
}

resource "aws_iam_policy" "vllm_wheels_bucket_read_write_access" {
  name = "read-write-access-to-vllm-wheels-bucket"

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action = [
        "s3:Get*",
        "s3:List",
        "s3:PutObject"
      ],
      Effect = "Allow",
      Resource = [
        "arn:aws:s3:::vllm-wheels/*",
        "arn:aws:s3:::vllm-wheels",
        "arn:aws:s3:::vllm-wheels-dev",
        "arn:aws:s3:::vllm-wheels-dev/*"
      ]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "premerge_ecr_public_read_access" {
  for_each   = merge(
    aws_cloudformation_stack.bk_queue_ci_gpu
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.premerge_ecr_public_read_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "premerge_ecr_public_write_access" {
  for_each   = merge(
    aws_cloudformation_stack.bk_queue,
    aws_cloudformation_stack.bk_queue_premerge,
    aws_cloudformation_stack.bk_queue_premerge_us_east_1,
    aws_cloudformation_stack.bk_queue_postmerge,
    aws_cloudformation_stack.bk_queue_postmerge_us_east_1,
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.premerge_ecr_public_write_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "premerge_ecr_cache_read_write_access" {
  for_each = merge(
    aws_cloudformation_stack.bk_queue,
    aws_cloudformation_stack.bk_queue_premerge,
    aws_cloudformation_stack.bk_queue_premerge_us_east_1,
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.premerge_ecr_cache_read_write_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "postmerge_ecr_public_read_access" {
  for_each   = merge(
    aws_cloudformation_stack.bk_queue_ci_gpu
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.postmerge_ecr_public_read_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "postmerge_ecr_public_read_write_access" {
  for_each   = merge(
    aws_cloudformation_stack.bk_queue_postmerge,
    aws_cloudformation_stack.bk_queue_postmerge_us_east_1,
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.postmerge_ecr_public_read_write_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "postmerge_ecr_cache_read_write_access" {
  for_each = merge(
    aws_cloudformation_stack.bk_queue_postmerge,
    aws_cloudformation_stack.bk_queue_postmerge_us_east_1,
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.postmerge_ecr_cache_read_write_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "release_ecr_public_read_write_access" {
  for_each   = merge(
    aws_cloudformation_stack.bk_queue_postmerge,
    aws_cloudformation_stack.bk_queue_postmerge_us_east_1,
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.release_ecr_public_read_write_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "cpu_release_ecr_public_read_write_access" {
  for_each   = merge(
    aws_cloudformation_stack.bk_queue_postmerge,
    aws_cloudformation_stack.bk_queue_postmerge_us_east_1,
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.cpu_release_ecr_public_read_write_access_policy.arn
}

resource "aws_iam_role_policy_attachment" "bk_stack_secrets_access" {
  for_each = merge(
    aws_cloudformation_stack.bk_queue,
    aws_cloudformation_stack.bk_queue_premerge,
    aws_cloudformation_stack.bk_queue_premerge_us_east_1,
    aws_cloudformation_stack.bk_queue_postmerge,
    aws_cloudformation_stack.bk_queue_postmerge_us_east_1,
    aws_cloudformation_stack.bk_queue_ci_gpu,
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.bk_stack_secrets_access.arn
}

resource "aws_iam_role_policy_attachment" "bk_stack_sccache_bucket_read_access" {
  for_each = merge(
    {
      for k, v in aws_cloudformation_stack.bk_queue_premerge : k => v
      if v.name == "bk-cpu-queue-premerge"
    },
    {
      for k, v in aws_cloudformation_stack.bk_queue_premerge : k => v
      if v.name == "bk-arm64-cpu-queue-premerge"
    },
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.bk_stack_sccache_bucket_read_access.arn
}

resource "aws_iam_role_policy_attachment" "bk_stack_sccache_bucket_read_write_access" {
  for_each = merge(
    {
      for k, v in aws_cloudformation_stack.bk_queue : k => v
      if v.name == "bk-cpu-queue"
    },
    {
      for k, v in aws_cloudformation_stack.bk_queue_postmerge : k => v
      if v.name == "bk-cpu-queue-postmerge"
    },
    {
      for k, v in aws_cloudformation_stack.bk_queue_postmerge : k => v
      if v.name == "bk-arm64-cpu-queue-postmerge"
    },
    {
      for k, v in aws_cloudformation_stack.bk_queue_postmerge_us_east_1 : k => v
      if v.name == "bk-cpu-queue-postmerge-us-east-1"
    }
  )
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.bk_stack_sccache_bucket_read_write_access.arn
}

resource "aws_iam_role_policy_attachment" "vllm_wheels_bucket_read_write_access" {
  for_each = {
    for k, v in aws_cloudformation_stack.bk_queue_postmerge : k => v
  }
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.vllm_wheels_bucket_read_write_access.arn
}

resource "aws_iam_role_policy_attachment" "vllm_wheels_bucket_read_write_access_us_east_1" {
  for_each = {
    for k, v in aws_cloudformation_stack.bk_queue_postmerge_us_east_1 : k => v
  }
  role       = each.value.outputs.InstanceRoleName
  policy_arn = aws_iam_policy.vllm_wheels_bucket_read_write_access.arn
}

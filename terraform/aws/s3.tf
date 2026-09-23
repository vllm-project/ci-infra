resource "aws_s3_bucket" "vllm_wheels" {
    bucket = "vllm-wheels"
}

resource "aws_s3_bucket" "vllm_wheels_dev" {
    bucket = "vllm-wheels-dev"
}

# Coverage tables for the CI test selector (buildkite/ci_selector): per
# commit, the per-step kernel table and the kernel symbol map, plus a
# latest.json pointer, written by the recording build's collect step
# (buildkite/ci_selector/recorders/kernrec/collect.sh). Public read: the bootstrap
# agents fetch over HTTPS without credentials, and the contents are step keys
# and kernel symbol names.
resource "aws_s3_bucket" "vllm_ci_selector" {
  bucket = "vllm-ci-selector"
}

resource "aws_s3_bucket_public_access_block" "vllm_ci_selector" {
  bucket                  = aws_s3_bucket.vllm_ci_selector.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "vllm_ci_selector_public_read" {
  bucket = aws_s3_bucket.vllm_ci_selector.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublicRead"
      Effect    = "Allow"
      Principal = "*"
      Action    = ["s3:GetObject"]
      Resource  = "${aws_s3_bucket.vllm_ci_selector.arn}/*"
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.vllm_ci_selector]
}

# A few MB per recording build; a year is plenty of history.
resource "aws_s3_bucket_lifecycle_configuration" "vllm_ci_selector" {
  bucket = aws_s3_bucket.vllm_ci_selector.id
  rule {
    id     = "expire-old-tables"
    status = "Enabled"
    filter {
      prefix = ""
    }
    expiration {
      days = 365
    }
  }
}

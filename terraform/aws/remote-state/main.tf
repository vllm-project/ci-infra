resource "random_id" "name_suffix" {
  byte_length = 8
}

locals {
  name = join("-", compact([
    var.state_prefix,
    "tfstate",
    random_id.name_suffix.hex
  ]))
}

resource "aws_s3_bucket" "state_bucket" {
  bucket = local.name

  versioning {
    enabled = true
  }

  server_side_encryption_configuration {
    rule {
      bucket_key_enabled = false
      apply_server_side_encryption_by_default {
        sse_algorithm = "AES256"
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "state_bucket" {
  bucket = aws_s3_bucket.state_bucket.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "state_lock_table" {
  name         = "terraform-state-lock"
  billing_mode = "PAY_PER_REQUEST"


  hash_key = "LockID"
  attribute {
    name = "LockID"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

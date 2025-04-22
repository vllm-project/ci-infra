output "bucket" {
  value = aws_s3_bucket.state_bucket.id
}

output "region" {
  value = aws_s3_bucket.state_bucket.region
}

output "dynamodb_table" {
  value = aws_dynamodb_table.state_lock_table.id
}

output "key" {
  value = "terraform.tfstate"
}

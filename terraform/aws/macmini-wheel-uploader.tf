# S3 write access to vllm-wheels for the self-hosted macOS wheel builder (the
# `macmini` queue), which has no EC2 instance role. See terraform/aws/README.

resource "aws_iam_user" "macmini_wheel_uploader" {
  name = "bk-macmini-wheel-uploader"
}

resource "aws_iam_user_policy_attachment" "macmini_wheels_bucket_rw" {
  user       = aws_iam_user.macmini_wheel_uploader.name
  policy_arn = aws_iam_policy.vllm_wheels_bucket_read_write_access.arn
}

resource "aws_iam_access_key" "macmini_wheel_uploader" {
  user = aws_iam_user.macmini_wheel_uploader.name
}

output "macmini_wheel_uploader_access_key_id" {
  value     = aws_iam_access_key.macmini_wheel_uploader.id
  sensitive = true
}

output "macmini_wheel_uploader_secret_access_key" {
  value     = aws_iam_access_key.macmini_wheel_uploader.secret
  sensitive = true
}

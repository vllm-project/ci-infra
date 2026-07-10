# Credentials for the macOS wheel builder (the `macmini` Buildkite queue).
#
# The Linux release builders are EC2 instances and get S3 write to vllm-wheels
# from their instance role (see vllm_wheels_bucket_read_write_access in iam.tf).
# The mac mini is a self-hosted agent with no instance profile, so it uses an
# IAM user access key instead, reusing that same least-privilege policy.
#
# The generated key is a long-lived secret and lands in Terraform state. After
# apply, retrieve it once and place it on the agent (see terraform/aws/README).

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

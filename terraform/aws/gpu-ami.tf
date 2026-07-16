resource "aws_ami_copy" "gpu_us_east_1" {
  provider = aws.us_east_1

  name              = "vllm-buildkite-stack-linux-gpu-us-east-1"
  description       = "vLLM Buildkite GPU AMI copied from us-west-2"
  source_ami_id     = "ami-040f1b73b7a7c7453"
  source_ami_region = "us-west-2"
  encrypted         = true

  tags = {
    Name = "vLLM Buildkite GPU AMI us-east-1"
  }
}

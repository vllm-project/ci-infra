resource "aws_s3_bucket" "vllm_wheels" {
    bucket = "vllm-wheels"
}

resource "aws_s3_bucket" "vllm_wheels_dev" {
    bucket = "vllm-wheels-dev"
}

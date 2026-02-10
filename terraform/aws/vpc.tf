module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.0.0"

  name = "vllm-ci-vpc"
  cidr = "10.0.0.0/16"

  azs            = ["us-west-2a", "us-west-2b", "us-west-2c", "us-west-2d"]
  public_subnets = ["10.0.0.0/18", "10.0.64.0/18", "10.0.128.0/18", "10.0.192.0/18"]

  enable_dns_hostnames          = true
  map_public_ip_on_launch       = true
  manage_default_network_acl    = false
  manage_default_route_table    = false
  manage_default_security_group = false

  tags = {
    Name = "vLLM CI VPC"
  }
}

module "vpc_us_east_1" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.0.0"

  name = "vllm-ci-vpc-us-east-1"
  cidr = "10.0.0.0/16"

  azs            = ["us-east-1a", "us-east-1b", "us-east-1c", "us-east-1d"]
  public_subnets = ["10.0.0.0/18", "10.0.64.0/18", "10.0.128.0/18", "10.0.192.0/18"]

  enable_dns_hostnames          = true
  map_public_ip_on_launch       = true
  manage_default_network_acl    = false
  manage_default_route_table    = false
  manage_default_security_group = false

  tags = {
    Name = "vLLM CI VPC us-east-1"
  }

  providers = {
    aws = aws.us_east_1
  }
}

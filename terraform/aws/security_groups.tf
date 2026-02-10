resource "aws_security_group" "ci-model-weights-sg" {
  name        = "ci-model-weights-security-group"
  description = "Security group for the CI model weights EFS"
  vpc_id      = module.vpc.vpc_id

  ingress = [
    {
      cidr_blocks      = ["10.0.0.0/16"]
      description      = "Allow inbound NFS from VPC"
      from_port        = 2049
      to_port          = 2049
      protocol         = "tcp"
      ipv6_cidr_blocks = []
      prefix_list_ids  = []
      security_groups  = []
      self             = false
    },
    {
      cidr_blocks      = []
      description      = null
      from_port        = 1018
      to_port          = 1023
      protocol         = "tcp"
      ipv6_cidr_blocks = []
      prefix_list_ids  = []
      security_groups  = ["sg-0c83698515e47eb5b"]
      self             = true
    },
    {
      cidr_blocks      = []
      description      = null
      from_port        = 988
      to_port          = 988
      protocol         = "tcp"
      ipv6_cidr_blocks = []
      prefix_list_ids  = []
      security_groups  = ["sg-0c83698515e47eb5b"]
      self             = true
    }
  ]

  egress = [
    {
      cidr_blocks      = ["0.0.0.0/0"]
      description      = null
      from_port        = 0
      to_port          = 0
      protocol         = "-1"
      ipv6_cidr_blocks = []
      prefix_list_ids  = []
      security_groups  = []
      self             = false
    },
    {
      cidr_blocks      = []
      description      = null
      from_port        = 1018
      to_port          = 1023
      protocol         = "tcp"
      ipv6_cidr_blocks = []
      prefix_list_ids  = []
      security_groups  = ["sg-0c83698515e47eb5b"]
      self             = true
    },
    {
      cidr_blocks      = []
      description      = null
      from_port        = 988
      to_port          = 988
      protocol         = "tcp"
      ipv6_cidr_blocks = []
      prefix_list_ids  = []
      security_groups  = ["sg-0c83698515e47eb5b"]
      self             = true
    }
  ]

  tags = {
    Name = "ci-model-weights-security-group"
  }
}

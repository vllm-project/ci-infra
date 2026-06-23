resource "aws_ec2_host" "mac_metal" {
  instance_type     = "mac2-m2pro.metal"
  availability_zone = "us-west-2a"
  auto_placement    = "on"

  tags = {
    Name = "mac-metal-dedicated-host"
  }
}

resource "aws_security_group" "mac_metal" {
  name        = "mac-metal-sg"
  description = "Security group for Mac Metal instance"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "mac-metal-sg"
  }
}

resource "aws_instance" "mac_metal" {
  ami           = "ami-063755aadeb97329a" # macOS Tahoe
  instance_type = "mac2-m2pro.metal"
  host_id       = aws_ec2_host.mac_metal.id
  subnet_id     = module.vpc.public_subnets[0]
  key_name      = var.mac_metal_key_name

  vpc_security_group_ids = [aws_security_group.mac_metal.id]

  root_block_device {
    volume_size = 500
    volume_type = "gp3"
  }

  tags = {
    Name = "mac-metal-instance"
  }
}

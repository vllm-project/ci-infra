variable "elastic_ci_stack_version" {
  type    = string
  default = "6.21.0"
}

variable "ci_hf_token" {
  type        = string
  description = "Huggingface token used to run CI tests"
}

variable "eks_admin_principal_arns" {
  type        = list(string)
  description = "IAM principal ARNs (e.g. SSO roles) granted AmazonEKSClusterAdminPolicy on the l4-ci cluster"
  default = [
    "arn:aws:iam::936637512419:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_AdminAccess_e8f1a24c3a971e07",
  ]
}

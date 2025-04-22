resource "aws_secretsmanager_secret" "ci_hf_token" {
  name = "ci_hf_token"
}

resource "aws_secretsmanager_secret_version" "ci_hf_token" {
  secret_id = aws_secretsmanager_secret.ci_hf_token.id
  secret_string = var.ci_hf_token
}

resource "aws_secretsmanager_secret" "bk_analytics_token" {
  name = "bk_analytics_token"
}

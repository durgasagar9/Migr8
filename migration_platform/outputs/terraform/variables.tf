variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "ami_id" {
  type = string
}

variable "lambda_exec_role_arn" {
  type = string
}

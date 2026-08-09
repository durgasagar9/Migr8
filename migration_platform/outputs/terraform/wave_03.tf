# Wave 3


resource "aws_lambda_function" "job_nightly_etl" {
  function_name = "job-nightly-etl"
  runtime       = "python3.9"
  handler       = "handler.main"
  filename      = "PLACEHOLDER_upload_deployment_package.zip"
  role          = var.lambda_exec_role_arn

  tags = {
    Name     = "nightly-etl-job"
    SourceId = "job-nightly-etl"
  }
}


resource "aws_instance" "vm_crm_app" {
  ami           = var.ami_id
  instance_type = "t3.large"

  tags = {
    Name           = "crm-app-server"
    SourceId       = "vm-crm-app"
    MigrationWave  = "3"
  }
}


resource "aws_instance" "vm_web_01" {
  ami           = var.ami_id
  instance_type = "t3.large"

  tags = {
    Name           = "orders-web-01"
    SourceId       = "vm-web-01"
    MigrationWave  = "3"
  }
}


resource "aws_instance" "vm_web_02" {
  ami           = var.ami_id
  instance_type = "t3.large"

  tags = {
    Name           = "orders-web-02"
    SourceId       = "vm-web-02"
    MigrationWave  = "3"
  }
}

# Wave 2


resource "aws_db_instance" "db_legacy_crm" {
  identifier        = "db-legacy-crm"
  engine            = "oracle-se2"
  allocated_storage = 2048
  instance_class    = "db.t3.large"
  multi_az          = false
  skip_final_snapshot = false
  final_snapshot_identifier = "db-legacy-crm-final-snapshot"

  tags = {
    Name     = "crm-oracle"
    SourceId = "db-legacy-crm"
  }
}


resource "aws_db_instance" "db_orders" {
  identifier        = "db-orders"
  engine            = "postgres"
  allocated_storage = 512
  instance_class    = "db.t3.large"
  multi_az          = false
  skip_final_snapshot = false
  final_snapshot_identifier = "db-orders-final-snapshot"

  tags = {
    Name     = "orders-postgres"
    SourceId = "db-orders"
  }
}


resource "aws_route53_zone" "dns_primary" {
  name = "corp.internal"

  tags = {
    Name     = "internal-dns"
    SourceId = "dns-primary"
  }
}


resource "aws_s3_bucket" "fs_reports" {
  bucket = "fs-reports-migrated"

  tags = {
    Name     = "reports-fileshare"
    SourceId = "fs-reports"
  }
}

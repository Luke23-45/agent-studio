# P6-9 — Neryva AWS baseline (terraform)
#
# Provision: VPC, KMS (master key + backup key), RDS Postgres 18 with
# pgvector + read replica, ElastiCache Redis (cluster mode), S3 (archive /
# eval corpora / exports), WAF attached to the ALB, and SSM parameters for
# secret references (ties P0-8: the KMS-injected master key).
#
# Usage:
#   terraform init
#   terraform plan -var environment=staging
#   terraform apply -var environment=staging

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  backend "s3" {}  # set bucket/key/region via `-backend-config`
}

variable "environment" {
  type    = string
  default = "staging"
}

variable "region" {
  type    = string
  default = "eu-west-1"
}

provider "aws" {
  region = var.region
}

locals {
  name = "neryva-${var.environment}"
  tags = {
    Project     = "neryva"
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

# ------------------------------------------------------------------
# Network
# ------------------------------------------------------------------
resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = local.tags
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.${count.index}.0/24"
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true
  tags                    = local.tags
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.${count.index + 10}.0/24"
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags              = local.tags
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = local.tags
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = local.tags
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "api" {
  name        = "${local.name}-api"
  vpc_id      = aws_vpc.main.id
  description = "ALB + API ingress"
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = local.tags
}

# ------------------------------------------------------------------
# KMS (ties P0-8: master key injected into services)
# ------------------------------------------------------------------
resource "aws_kms_key" "master" {
  description             = "${local.name} provider-secret master key (P0-8 kms_ref)"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.tags
}

resource "aws_kms_alias" "master" {
  name          = "alias/${local.name}-master"
  target_key_id = aws_kms_key.master.key_id
}

resource "aws_kms_key" "backups" {
  description             = "${local.name} backup encryption key (RDS/S3)"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.tags
}

resource "aws_kms_alias" "backups" {
  name          = "alias/${local.name}-backups"
  target_key_id = aws_kms_key.backups.key_id
}

# ------------------------------------------------------------------
# RDS Postgres 18 + pgvector (multi-AZ, PITR, read replica)
# ------------------------------------------------------------------
resource "aws_db_parameter_group" "pgvector" {
  name   = "${local.name}-pgvector"
  family = "postgres18"
  parameter {
    name         = "shared_preload_libraries"
    value        = "vector"
    apply_method = "pending-reboot"
  }
}

resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-db"
  subnet_ids = aws_subnet.private[*].id
  tags       = local.tags
}

resource "aws_db_instance" "primary" {
  identifier                  = "${local.name}-postgres"
  engine                      = "postgres"
  engine_version              = "18"
  instance_class              = "db.t3.medium"
  allocated_storage           = 100
  max_allocated_storage       = 500
  storage_type                = "gp3"
  storage_encrypted           = true
  kms_key_id                  = aws_kms_key.backups.arn
  db_name                     = "neryva"
  username                    = "neryva"
  password                    = random_password.db_master.result
  db_subnet_group_name        = aws_db_subnet_group.main.name
  parameter_group_name        = aws_db_parameter_group.pgvector.name
  multi_az                    = true
  backup_retention_period     = 30
  backup_window               = "02:00-03:00"
  maintenance_window          = "sun:04:00-sun:05:00"
  performance_insights_enabled = true
  deletion_protection         = var.environment == "production" ? true : false
  skip_final_snapshot         = var.environment == "production" ? false : true
  tags                        = local.tags
}

resource "aws_db_instance" "replica" {
  count                     = 1
  identifier                = "${local.name}-postgres-replica"
  replicate_source_db       = aws_db_instance.primary.identifier
  instance_class            = "db.t3.medium"
  storage_encrypted         = true
  kms_key_id                = aws_kms_key.backups.arn
  parameter_group_name      = aws_db_parameter_group.pgvector.name
  performance_insights_enabled = true
  tags                      = local.tags
}

resource "random_password" "db_master" {
  length  = 24
  special = false
}

# ------------------------------------------------------------------
# ElastiCache Redis (cluster mode, encryption, multi-AZ)
# ------------------------------------------------------------------
resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.name}-redis"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id          = "${local.name}-redis"
  description                   = "${local.name} Redis cluster"
  engine                        = "redis"
  engine_version                = "7.1"
  node_type                     = "cache.t3.micro"
  num_node_groups               = 2
  replicas_per_node_group       = 1
  automatic_failover_enabled    = true
  multi_az_enabled              = true
  subnet_group_name             = aws_elasticache_subnet_group.main.name
  security_group_ids            = [aws_security_group.redis.id]
  at_rest_encryption_enabled    = true
  transit_encryption_enabled    = true
  kms_key_id                    = aws_kms_key.backups.arn
  snapshot_retention_limit      = 7
  snapshot_window               = "03:00-04:00"
  parameter_group_name          = aws_elasticache_parameter_group.redis.name
  tags                          = local.tags
}

resource "aws_elasticache_parameter_group" "redis" {
  name   = "${local.name}-redis"
  family = "redis7"
}

resource "aws_security_group" "redis" {
  name   = "${local.name}-redis"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.api.id]
  }
  tags = local.tags
}

# ------------------------------------------------------------------
# S3 (archive tier, eval corpora, DSR exports — versioned, encrypted)
# ------------------------------------------------------------------
resource "aws_s3_bucket" "archive" {
  bucket        = "${local.name}-archives"
  force_destroy = var.environment != "production"
  tags          = local.tags
}

resource "aws_s3_bucket_versioning" "archive" {
  bucket = aws_s3_bucket.archive.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id
  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.backups.arn
      sse_algorithm     = "aws:kms"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id
  rule {
    id     = "cold-tier"
    status = "Enabled"
    transitions {
      days          = 90
      storage_class = "GLACIER_IR"
    }
  }
}

resource "aws_s3_bucket" "evals" {
  bucket        = "${local.name}-eval-corpora"
  force_destroy = var.environment != "production"
  tags          = local.tags
}

resource "aws_s3_bucket_versioning" "evals" {
  bucket = aws_s3_bucket.evals.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evals" {
  bucket = aws_s3_bucket.evals.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
      kms_master_key_id = aws_kms_key.backups.arn
    }
  }
}

resource "aws_s3_bucket" "exports" {
  bucket        = "${local.name}-exports"
  force_destroy = var.environment != "production"
  tags          = local.tags
}

resource "aws_s3_bucket_versioning" "exports" {
  bucket = aws_s3_bucket.exports.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "exports" {
  bucket = aws_s3_bucket.exports.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.backups.arn
    }
  }
}

# ------------------------------------------------------------------
# ALB + WAF (AWS managed rules + rate limiting)
# ------------------------------------------------------------------
resource "aws_lb" "main" {
  name               = "${local.name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.api.id]
  subnets            = aws_subnet.public[*].id
  tags               = local.tags
}

resource "aws_wafv2_web_acl" "main" {
  name        = "${local.name}-waf"
  scope       = "REGIONAL"
  description = "WAF baseline: managed SQLi/XSS + rate limiting"
  default_action {
    allow {}
  }
  rule {
    name     = "aws-managed-core"
    priority = 1
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}Core"
      sampled_requests_enabled   = true
    }
  }
  rule {
    name     = "aws-managed-sqli"
    priority = 2
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesSQLiRuleSet"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}Sqli"
      sampled_requests_enabled   = true
    }
  }
  rule {
    name     = "rate-limit"
    priority = 3
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = 2000
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}RateLimit"
      sampled_requests_enabled   = true
    }
  }
  tags = local.tags
}

resource "aws_wafv2_web_acl_association" "main" {
  resource_arn = aws_lb.main.arn
  web_acl_arn  = aws_wafv2_web_acl.main.arn
}

# ------------------------------------------------------------------
# SSM (secret references; values injected via env/KMS at deploy time)
# ------------------------------------------------------------------
resource "aws_ssm_parameter" "db_username" {
  name  = "/${local.name}/db/username"
  type  = "SecureString"
  value = aws_db_instance.primary.username
  key_id = aws_kms_key.master.key_id
  tags  = local.tags
}

resource "aws_ssm_parameter" "db_password" {
  name   = "/${local.name}/db/password"
  type   = "SecureString"
  value  = random_password.db_master.result
  key_id = aws_kms_key.master.key_id
  tags   = local.tags
}

output "db_endpoint" {
  value = aws_db_instance.primary.endpoint
}

output "redis_endpoint" {
  value = aws_elasticache_replication_group.redis.primary_endpoint_address
}

output "alb_dns" {
  value = aws_lb.main.dns_name
}

output "master_key_id" {
  value = aws_kms_key.master.key_id
}

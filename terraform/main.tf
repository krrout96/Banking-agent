terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}


data "aws_partition" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  app_name            = var.app_name
  ecs_cluster_name    = "${local.app_name}-cluster"
  ecs_service_name    = "${local.app_name}-service"
  ecs_task_family     = "${local.app_name}-task"
  ecr_repository_name = local.app_name
  tools_lambda_name   = "${local.app_name}-tools"
  lambda_role_name    = "${local.app_name}-lambda-role"
  execution_role_name = "${local.app_name}-execution-role"
  task_role_name      = "${local.app_name}-task-role"
  diagram_bucket_name = "${local.app_name}-diagrams-${data.aws_caller_identity.current.account_id}"
  container_name      = local.app_name
  container_port      = 8000
  ecs_log_group_name  = "/ecs/${local.app_name}"
  target_group_name   = substr("${local.app_name}-tg", 0, 32)
  alb_name            = substr("${local.app_name}-alb", 0, 32)
  image_uri           = "${aws_ecr_repository.app.repository_url}:${var.container_image_tag}"
  lambda_source_file  = "${path.module}/../mig_assist/backend/tools_lambda.py"
  lambda_output_zip   = "${path.module}/tools_lambda.zip"
  bedrock_tools_openapi_schema = jsonencode({
    openapi = "3.0.1"
    info = {
      title   = "MigrationTools"
      version = "1.0.0"
    }
    paths = {
      "/cost-assistant" = {
        post = {
          operationId = "cost_assistant"
          description = "Returns AWS pricing guidance for a service payload."
          requestBody = {
            required = true
            content = {
              "application/json" = {
                schema = {
                  type = "object"
                  properties = {
                    payload = { type = "string" }
                  }
                  required = ["payload"]
                }
              }
            }
          }
          responses = {
            "200" = { description = "Tool response" }
          }
        }
      }
      "/aws-docs-assistant" = {
        post = {
          operationId = "aws_docs_assistant"
          description = "Returns AWS documentation guidance for a query."
          requestBody = {
            required = true
            content = {
              "application/json" = {
                schema = {
                  type = "object"
                  properties = {
                    payload = { type = "string" }
                  }
                  required = ["payload"]
                }
              }
            }
          }
          responses = {
            "200" = { description = "Tool response" }
          }
        }
      }
      "/vpc-subnet-calculator" = {
        post = {
          operationId = "vpc_subnet_calculator"
          description = "Calculates subnet layout for a CIDR block."
          requestBody = {
            required = true
            content = {
              "application/json" = {
                schema = {
                  type = "object"
                  properties = {
                    cidr = { type = "string" }
                  }
                  required = ["cidr"]
                }
              }
            }
          }
          responses = {
            "200" = { description = "Tool response" }
          }
        }
      }
    }
  })
}

resource "aws_s3_bucket" "diagrams" {
  bucket = local.diagram_bucket_name
}

resource "aws_s3_bucket_public_access_block" "diagrams" {
  bucket = aws_s3_bucket.diagrams.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_cors_configuration" "diagrams" {
  bucket = aws_s3_bucket.diagrams.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET"]
    allowed_origins = ["*"]
    max_age_seconds = 3600
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "diagrams" {
  bucket = aws_s3_bucket.diagrams.id

  rule {
    id     = "DeleteOldDiagrams"
    status = "Enabled"

    filter {
      prefix = "diagrams/"
    }

    expiration {
      days = 1
    }
  }
}

resource "aws_ecr_repository" "app" {
  name                 = local.ecr_repository_name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep only the latest 25 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 25
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "ecs" {
  name              = local.ecs_log_group_name
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "ecs_execution" {
  name = local.execution_role_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "ecs_task" {
  name = local.task_role_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "ecs_task_inline" {
  name = "MigrationAgentPolicy"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.diagrams.arn,
          "${aws_s3_bucket.diagrams.arn}/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:InvokeAgent"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [aws_lambda_function.tools.arn]
      }
    ]
  })
}

resource "aws_iam_role" "tools_lambda" {
  name = local.lambda_role_name

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "tools_lambda_basic" {
  role       = aws_iam_role.tools_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "tools_lambda_pricing" {
  name = "PricingAccess"
  role = aws_iam_role.tools_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["pricing:GetProducts", "pricing:GetAttributeValues"]
        Resource = "*"
      }
    ]
  })
}

data "archive_file" "tools_lambda" {
  type        = "zip"
  output_path = local.lambda_output_zip

  source {
    content  = file(local.lambda_source_file)
    filename = "lambda_function.py"
  }
}

resource "aws_lambda_function" "tools" {
  function_name = local.tools_lambda_name
  role          = aws_iam_role.tools_lambda.arn
  handler       = "lambda_function.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 128

  filename         = data.archive_file.tools_lambda.output_path
  source_code_hash = data.archive_file.tools_lambda.output_base64sha256

  depends_on = [aws_iam_role_policy_attachment.tools_lambda_basic]
}

resource "aws_iam_role" "bedrock_agent" {
  count = var.create_bedrock_agent ? 1 : 0

  name = "${local.app_name}-bedrock-agent-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "bedrock.amazonaws.com"
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
          ArnLike = {
            "AWS:SourceArn" = "arn:${data.aws_partition.current.partition}:bedrock:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:agent/*"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "bedrock_agent" {
  count = var.create_bedrock_agent ? 1 : 0

  name = "${local.app_name}-bedrock-agent-policy"
  role = aws_iam_role.bedrock_agent[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:GetInferenceProfile",
          "bedrock:ListInferenceProfiles"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "lambda:InvokeFunction"
        ]
        Resource = [
          aws_lambda_function.tools.arn
        ]
      }
    ]
  })
}

resource "aws_bedrockagent_agent" "migration" {
  count = var.create_bedrock_agent ? 1 : 0

  agent_name                  = "${local.app_name}-bedrock-agent"
  agent_resource_role_arn     = aws_iam_role.bedrock_agent[0].arn
  foundation_model            = var.bedrock_foundation_model
  instruction                 = var.bedrock_agent_instruction
  idle_session_ttl_in_seconds = var.bedrock_idle_session_ttl_in_seconds
  prepare_agent               = true
}

resource "aws_lambda_permission" "bedrock_agent_tools" {
  count = var.create_bedrock_agent ? 1 : 0

  statement_id   = "AllowBedrockAgentInvokeTools"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.tools.function_name
  principal      = "bedrock.amazonaws.com"
  source_account = data.aws_caller_identity.current.account_id
  source_arn     = "arn:${data.aws_partition.current.partition}:bedrock:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:agent/${aws_bedrockagent_agent.migration[0].agent_id}"
}

resource "aws_lambda_permission" "bedrock_agent_tools_alias" {
  count = var.create_bedrock_agent ? 1 : 0

  statement_id   = "AllowBedrockAgentAliasInvokeTools"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.tools.function_name
  principal      = "bedrock.amazonaws.com"
  source_account = data.aws_caller_identity.current.account_id
  source_arn     = "arn:${data.aws_partition.current.partition}:bedrock:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:agent-alias/${aws_bedrockagent_agent.migration[0].agent_id}/*"
}

resource "aws_bedrockagent_agent_action_group" "tools" {
  count = var.create_bedrock_agent ? 1 : 0

  agent_id                   = aws_bedrockagent_agent.migration[0].agent_id
  agent_version              = "DRAFT"
  action_group_name          = var.bedrock_tools_action_group_name
  description                = "Lambda-backed toolset for migration assistant"
  skip_resource_in_use_check = true

  action_group_executor {
    lambda = aws_lambda_function.tools.arn
  }

  api_schema {
    payload = local.bedrock_tools_openapi_schema
  }

  depends_on = [
    aws_lambda_permission.bedrock_agent_tools,
    aws_lambda_permission.bedrock_agent_tools_alias
  ]
}

resource "aws_bedrockagent_agent_alias" "migration" {
  count = var.create_bedrock_agent ? 1 : 0

  agent_alias_name = var.bedrock_agent_alias_name
  agent_id         = aws_bedrockagent_agent.migration[0].agent_id
  description      = "Alias for ${local.app_name} Bedrock Agent"

  depends_on = [aws_bedrockagent_agent_action_group.tools]
}

resource "aws_ecs_cluster" "app" {
  name = local.ecs_cluster_name
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${local.app_name}-vpc"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${local.app_name}-igw"
  }
}

resource "aws_subnet" "public" {
  count = length(var.public_subnet_cidrs)

  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = element(data.aws_availability_zones.available.names, count.index)
  map_public_ip_on_launch = true

  tags = {
    Name = "${local.app_name}-public-${count.index + 1}"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${local.app_name}-public-rt"
  }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

resource "aws_route_table_association" "public" {
  count = length(var.public_subnet_cidrs)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "alb" {
  name        = "${local.app_name}-alb-sg"
  description = "ALB security group"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP"
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
}

resource "aws_security_group" "ecs" {
  name        = "${local.app_name}-ecs-sg"
  description = "ECS task security group"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "App traffic from ALB"
    from_port       = local.container_port
    to_port         = local.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "app" {
  name               = local.alb_name
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
  idle_timeout       = 360
}

resource "aws_lb_target_group" "app" {
  name        = local.target_group_name
  port        = local.container_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    protocol            = "HTTP"
    path                = "/"
    matcher             = "200-499"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }
}

resource "aws_lb_listener" "http_forward" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

resource "aws_ecs_task_definition" "app" {
  family                   = local.ecs_task_family
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.task_cpu)
  memory                   = tostring(var.task_memory)
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = local.container_name
      image     = local.image_uri
      essential = true
      portMappings = [
        {
          containerPort = local.container_port
          protocol      = "tcp"
        }
      ]
      environment = [
        { name = "DIAGRAM_BUCKET_NAME", value = aws_s3_bucket.diagrams.bucket },
        { name = "GATEWAY_URL", value = var.gateway_url },
        { name = "TOOLS_LAMBDA_NAME", value = aws_lambda_function.tools.function_name },
        { name = "BEDROCK_AGENT_ID", value = var.create_bedrock_agent ? aws_bedrockagent_agent.migration[0].agent_id : "" },
        { name = "BEDROCK_AGENT_ALIAS_ID", value = var.create_bedrock_agent ? aws_bedrockagent_agent_alias.migration[0].agent_alias_id : "" },
        { name = "AWS_DEFAULT_REGION", value = data.aws_region.current.name }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.ecs.name
          awslogs-region        = data.aws_region.current.name
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "app" {
  name            = local.ecs_service_name
  cluster         = aws_ecs_cluster.app.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  force_new_deployment = var.force_new_deployment

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = local.container_name
    container_port   = local.container_port
  }

  depends_on = [
    aws_lb_listener.http_forward
  ]
}

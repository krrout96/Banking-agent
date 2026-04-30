variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "app_name" {
  description = "Base name prefix for resources"
  type        = string
  default     = "migration-agent-cloud"
}

variable "create_bedrock_agent" {
  description = "Whether to create an Amazon Bedrock Agent and alias"
  type        = bool
  default     = true
}

variable "bedrock_foundation_model" {
  description = "Foundation model for Bedrock Agent orchestration"
  type        = string
  default     = "us.amazon.nova-pro-v1:0"
}

variable "bedrock_agent_instruction" {
  description = "Instruction prompt used by the Bedrock Agent"
  type        = string
  default     = "You are an AWS migration assistant. Help users assess architecture, recommend migration patterns, and guide them through implementation steps."
}

variable "bedrock_idle_session_ttl_in_seconds" {
  description = "Bedrock Agent session TTL in seconds"
  type        = number
  default     = 900
}

variable "bedrock_agent_alias_name" {
  description = "Alias name for Bedrock Agent"
  type        = string
  default     = "prod"
}

variable "bedrock_tools_action_group_name" {
  description = "Action group name used to attach the tools Lambda to the Bedrock Agent"
  type        = string
  default     = "migration_tools"
}


variable "vpc_cidr" {
  description = "CIDR block for the new VPC"
  type        = string
  default     = "10.50.0.0/16"
}

variable "public_subnet_cidrs" {

  description = "CIDR blocks for public subnets (use at least two in different AZs for ALB)"
  type        = list(string)
  default     = ["10.50.1.0/24", "10.50.2.0/24"]

  validation {
    condition = length(var.public_subnet_cidrs) >= 2


    error_message = "Provide at least two public subnet CIDRs for the ALB."
  }
}

variable "gateway_url" {
  description = "Gateway URL injected into ECS task environment"
  type        = string
  default     = ""
}

variable "container_image_tag" {
  description = "ECR image tag to deploy"
  type        = string
  default     = "latest"
}

variable "task_cpu" {
  description = "Fargate task CPU units"
  type        = number
  default     = 1024
}

variable "task_memory" {
  description = "Fargate task memory (MiB)"
  type        = number
  default     = 3072
}

variable "desired_count" {
  description = "Desired ECS task count (set to 0 for infra-first bootstrap, then scale to 1 after image push)"
  type        = number
  default     = 0
}

variable "log_retention_days" {
  description = "CloudWatch log retention for ECS logs"
  type        = number
  default     = 14
}

variable "force_new_deployment" {
  description = "Force ECS to redeploy tasks on apply"
  type        = bool
  default     = false
}

# AWS Migration Assistant

An AI-powered migration assistant that helps engineers plan, assess, and visualise AWS cloud migrations. It combines a React chat frontend, a Python Strands agent backend, and a set of AWS-backed tools — all deployed on ECS Fargate behind an ALB, with infrastructure managed entirely by Terraform.

---

## Architecture Overview

```
User Browser
    │
    ▼
ALB (port 80)
    │
    ▼
ECS Fargate Container (port 8000)
    ├── nginx  ──────────────────────────► React Frontend (static)
    │           /invocations              /diagrams/ (generated PNGs)
    └── Python Agent (port 8081)
            │
            ├── Amazon Bedrock Agent Runtime  (general Q&A)
            ├── Nova Pro (diagram JSON + image analysis)
            ├── diagrams library (PNG rendering with AWS icons)
            ├── Lambda: tools_lambda  ──────► AWS Pricing API
            │                                AWS Docs search
            │                                VPC subnet calculator
            └── S3 bucket  (generated diagram storage)
```

---

## AI Models Used

| Model | ID | Purpose |
|---|---|---|
| Amazon Nova Pro | `us.amazon.nova-pro-v1:0` | Main agent orchestration, architecture JSON extraction, HLD/LLD image analysis |
| Amazon Bedrock Agent Runtime | Managed agent (created by Terraform) | General migration Q&A when no diagram/image is involved |

### How Nova Pro is used

- **Architecture extraction** — given a user's natural language description, Nova Pro returns a structured JSON object (`title`, `clusters`, `connections`) that is then rendered as a PNG diagram
- **Image analysis** — when a user uploads an HLD/LLD diagram image, Nova Pro's vision capability analyses it and extracts AWS-equivalent services, security considerations, and migration recommendations
- **Agent orchestration** — the Strands agent uses Nova Pro as its foundation model to decide which tools to call (`cost_assistant`, `aws_docs_assistant`, `vpc_subnet_calculator`)

---

## Tools & Services

### Agent Tools

| Tool | Backed by | What it does |
|---|---|---|
| `arch_diag_assistant` | `diagrams` library + matplotlib | Generates AWS architecture diagrams as PNG with real AWS service icons |
| `hld_lld_input_agent` | Nova Pro vision | Analyses uploaded HLD/LLD architecture images |
| `cost_assistant` | AWS Pricing API (`pricing:GetProducts`) | Returns real-time pricing for EC2, RDS, Lambda, S3, DynamoDB, ECS, ELB |
| `aws_docs_assistant` | docs.aws.amazon.com search | Returns relevant AWS documentation links for a query |
| `vpc_subnet_calculator` | Pure Python (`ipaddress`) | Calculates optimal subnet CIDR splits for a given VPC CIDR, AZ count, and tier layout |

### AWS Services

| Service | Role |
|---|---|
| ECS Fargate | Runs the containerised agent + frontend |
| ECR | Stores the Docker image |
| ALB | Public HTTP entry point, routes to ECS |
| S3 | Stores generated architecture diagram PNGs (1-day lifecycle) |
| Lambda | Hosts the tools (pricing, docs, subnet calculator) |
| Amazon Bedrock | Nova Pro model + managed Bedrock Agent |
| CloudWatch Logs | ECS task logs (14-day retention) |
| IAM | Execution role (ECR/logs) + task role (S3, Bedrock, Lambda) |
| VPC | Dedicated VPC with public subnets across 2 AZs |

### Diagram Rendering Stack

1. **`diagrams` library** (preferred) — renders with official AWS service icons using Graphviz. Available after Docker rebuild.
2. **matplotlib** (fallback) — renders colour-coded service boxes with arrows. Works without Graphviz.

---

## Prerequisites

- AWS CLI v2 configured (`aws configure`)
- Terraform >= 1.5.0
- Docker Desktop
- Python 3.11+
- Node.js 22+
- Graphviz (for local diagram rendering with AWS icons)
  - Windows: `winget install graphviz`
  - Linux/Mac: `apt install graphviz` / `brew install graphviz`

---

## Deployment

### Step 1 — Enable Bedrock model access

In the AWS Console → Amazon Bedrock → Model access, enable:
- **Amazon Nova Pro** (`us.amazon.nova-pro-v1:0`)

### Step 2 — Deploy infrastructure with Terraform

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars if needed (region, app_name, etc.)

terraform init
terraform apply
```

Key outputs after apply:

| Output | Description |
|---|---|
| `ecr_repository_url` | ECR URL to push the Docker image to |
| `app_url` | ALB DNS — the application URL |
| `diagram_bucket_name` | S3 bucket for generated diagrams |
| `bedrock_agent_id` | Bedrock Agent ID (injected into ECS env) |
| `bedrock_agent_alias_id` | Bedrock Agent Alias ID |
| `tools_lambda_name` | Lambda function name for the tools |

> `desired_count` defaults to `0` on first apply (infra-first bootstrap). The ECS service will scale to 1 automatically after the image is pushed in Step 3.

### Step 3 — Build and deploy the application

Create `mig_assist/.env`:

```env
APP_NAME=migration-agent-cloud
APP_TITLE=AWS Migration Assistant
AWS_DEFAULT_REGION=us-east-1
DESIRED_COUNT=1
```

**Windows (PowerShell):**
```powershell
cd mig_assist
.\deploy.ps1
```

**Linux / Mac:**
```bash
cd mig_assist
./deploy.sh
```

The deploy script:
1. Logs into ECR
2. Builds the Docker image (`linux/amd64` for Fargate)
3. Pushes to ECR
4. Forces a new ECS deployment
5. Scales the service to `DESIRED_COUNT`

### Step 4 — Access the application

```bash
terraform output app_url
```

Open the URL in a browser. Allow 2–3 minutes for the ECS task to stabilise after first deploy.

---

## Terraform Variables

| Variable | Default | Description |
|---|---|---|
| `aws_region` | `us-east-1` | AWS region |
| `app_name` | `migration-agent-cloud` | Resource name prefix |
| `create_bedrock_agent` | `true` | Whether to create a managed Bedrock Agent |
| `bedrock_foundation_model` | `us.amazon.nova-pro-v1:0` | Foundation model for the Bedrock Agent |
| `vpc_cidr` | `10.50.0.0/16` | VPC CIDR block |
| `public_subnet_cidrs` | `["10.50.1.0/24","10.50.2.0/24"]` | Public subnet CIDRs (min 2 for ALB) |
| `task_cpu` | `1024` | Fargate task CPU units |
| `task_memory` | `3072` | Fargate task memory (MiB) |
| `desired_count` | `0` | ECS task count (set to 0 for bootstrap) |
| `log_retention_days` | `14` | CloudWatch log retention |
| `gateway_url` | `""` | Optional AgentCore Gateway URL |

---

## Infrastructure Resources Created by Terraform

- VPC with 2 public subnets across different AZs
- Internet Gateway + route table
- ALB with HTTP listener (port 80), target group, security groups
- ECS Cluster + Fargate task definition + service
- ECR repository (with lifecycle policy — keeps last 25 images)
- S3 bucket for diagrams (1-day expiry on `diagrams/` prefix, CORS enabled)
- Lambda function (`{app_name}-tools`) with IAM role + pricing policy
- Bedrock Agent + action group + alias (optional, controlled by `create_bedrock_agent`)
- CloudWatch log group (`/ecs/{app_name}`, 14-day retention)
- IAM roles: ECS execution role, ECS task role, Lambda role, Bedrock Agent role

---

## Project Structure

```
mig_assist/
├── backend/
│   ├── migration_agent.py      # Main agent — routing, tools, diagram generation
│   ├── tools_lambda.py         # Lambda: cost, docs, subnet calculator tools
│   ├── gateway_infra_utils.py  # AgentCore Gateway auth helper
│   ├── nginx.conf              # Nginx config (proxy + static file serving)
│   ├── supervisord.conf        # Process supervisor (nginx + agent)
│   └── requirements.pip        # Python dependencies
├── frontend/
│   └── src/
│       ├── App.jsx             # Chat UI, API calls, session management
│       └── components/Chat/
│           ├── MessageBubble.jsx   # Markdown rendering, diagram display, download
│           ├── InputArea.jsx       # Text + image upload input
│           └── TypingIndicator.jsx
├── Dockerfile                  # Multi-stage build (Node frontend + Python backend)
├── deploy.sh                   # Linux/Mac deploy script
└── deploy.ps1                  # Windows deploy script

terraform/
├── main.tf                     # All AWS resources
├── variables.tf                # Input variables
├── outputs.tf                  # Output values
└── terraform.tfvars.example    # Example variable values
```

---

## Updating the Application

After any code change, redeploy with:

```powershell
# Windows
.\deploy.ps1

# Linux/Mac
./deploy.sh
```

To apply infrastructure changes only:

```bash
cd terraform
terraform apply
```

---

## Logs

ECS task logs are in CloudWatch under `/ecs/{app_name}`. Key log prefixes:

| Prefix | Meaning |
|---|---|
| `[diagram]` | Diagram generation steps (JSON extraction, render) |
| `[diagrams]` | diagrams library render result |
| `[S3]` | Diagram upload to S3 |
| `[Local]` | Diagram saved to local static dir |
| `[Config]` | Startup config (bucket name, etc.) |

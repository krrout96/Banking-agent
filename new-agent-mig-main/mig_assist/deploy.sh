#!/bin/bash
set -e

# Ensure we run from the project root (where this script is)
cd "$(dirname "$0")"

# Load .env variables if present
# Load .env variables only if not already set in environment
# Load .env variables if present
if [ -f ".env" ]; then
    echo "[INFO] Loading configuration from .env"
    set -a
    source .env
    set +a
fi

APP_NAME="${APP_NAME}" # Must be provided via environment or .env
APP_TITLE="${APP_TITLE:-AWS Migration Assistant}" # Frontend Title
ECS_CLUSTER_NAME="${APP_NAME}-cluster"
ECS_SERVICE_NAME="${APP_NAME}-service"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
DESIRED_COUNT="${DESIRED_COUNT:-1}"

if [ -z "${APP_NAME}" ]; then
    echo "[ERROR] APP_NAME is not set. Set it in .env or export APP_NAME before running deploy.sh."
    exit 1
fi
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${APP_NAME}"

echo "[INFO] Deploying ${APP_NAME} to AWS in region ${AWS_REGION}..."
echo "[INFO] App Title: ${APP_TITLE}"
echo "[INFO] Cluster: ${ECS_CLUSTER_NAME}, Service: ${ECS_SERVICE_NAME}"

# 1. Login to ECR
echo "[INFO] Logging into ECR..."
aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

# 2. Check/Create Repository
echo "[INFO] Checking ECR Repository..."
aws ecr describe-repositories --repository-names ${APP_NAME} --region ${AWS_REGION} || \
aws ecr create-repository --repository-name ${APP_NAME} --region ${AWS_REGION}

# 3. Build Docker Image (Context is Root)
echo "[INFO] Building Docker Image (Targeting linux/amd64 for Fargate)..."
docker build --platform linux/amd64 \
  --build-arg VITE_APP_TITLE="${APP_TITLE}" \
  --build-arg VITE_COGNITO_USER_POOL_ID="${VITE_COGNITO_USER_POOL_ID}" \
  --build-arg VITE_COGNITO_CLIENT_ID="${VITE_COGNITO_CLIENT_ID}" \
  -f Dockerfile -t ${APP_NAME}:latest .
docker tag ${APP_NAME}:latest ${ECR_REPO_URI}:latest

# 4. Push to ECR
echo "[INFO] Pushing to ECR (This may take a while)..."
docker push ${ECR_REPO_URI}:latest

# 5. Force Update ECS Service
echo "[INFO] Updating ECS Service to pull new image..."
aws ecs update-service --cluster ${ECS_CLUSTER_NAME} --service ${ECS_SERVICE_NAME} --force-new-deployment --region ${AWS_REGION} > /dev/null
aws ecs update-service --cluster ${ECS_CLUSTER_NAME} --service ${ECS_SERVICE_NAME} --desired-count ${DESIRED_COUNT} --region ${AWS_REGION} > /dev/null

# 6. Retrieve URL
echo "[INFO] Retrieving Application URL..."
if [ -n "$DOMAIN_NAME" ]; then
    APP_URL="https://${DOMAIN_NAME}"
else
    # Fetch ALB DNS
    # We assume ALB name follows naming convention from provision.py: ${APP_NAME}-alb
    ALB_NAME="${APP_NAME}-alb"
    ALB_DNS=$(aws elbv2 describe-load_balancers --names ${ALB_NAME} --region ${AWS_REGION} --query "LoadBalancers[0].DNSName" --output text 2>/dev/null || echo "")
    
    if [ -n "$ALB_DNS" ]; then
        if [ -n "$ACM_CERT_ARN" ]; then
             APP_URL="https://${ALB_DNS}"
        else
             APP_URL="http://${ALB_DNS}"
        fi
    else
        APP_URL="Unable to retrieve URL. Check AWS Console."
    fi
fi

echo "[SUCCESS] Deployment Artifact Pushed & Service Updated!"
echo "Image URI: ${ECR_REPO_URI}:latest"
echo "Visit: ${APP_URL} (Give it 2-3 mins to stabilize)"

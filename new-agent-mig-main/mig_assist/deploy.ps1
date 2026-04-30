Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [Parameter(Mandatory = $false)]
        [string[]]$Arguments = @()
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $Command $($Arguments -join ' ')"
    }
}

# Ensure script runs from project root
Set-Location -Path $PSScriptRoot

# Load .env variables if present
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Write-Host "[INFO] Loading configuration from .env"
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $parts = $line -split "=", 2
        if ($parts.Count -eq 2) {
            $key = $parts[0].Trim()
            $value = $parts[1].Trim().Trim("'`"")
            if (-not [string]::IsNullOrWhiteSpace($key)) {
                Set-Item -Path "Env:$key" -Value $value
            }
        }
    }
}

$appName = $env:APP_NAME
$appTitle = if ([string]::IsNullOrWhiteSpace($env:APP_TITLE)) { "AWS Migration Assistant" } else { $env:APP_TITLE }
$awsRegion = if (-not [string]::IsNullOrWhiteSpace($env:AWS_REGION)) { $env:AWS_REGION } elseif (-not [string]::IsNullOrWhiteSpace($env:AWS_DEFAULT_REGION)) { $env:AWS_DEFAULT_REGION } else { "us-east-1" }
$desiredCount = if ([string]::IsNullOrWhiteSpace($env:DESIRED_COUNT)) { "1" } else { $env:DESIRED_COUNT }

if ([string]::IsNullOrWhiteSpace($appName)) {
    throw "[ERROR] APP_NAME is not set. Set it in .env or as an environment variable before running deploy.ps1."
}

$ecsClusterName = "$appName-cluster"
$ecsServiceName = "$appName-service"
$accountId = (& aws sts get-caller-identity --query Account --output text).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($accountId)) {
    throw "[ERROR] Failed to resolve AWS account id. Check AWS CLI credentials/profile."
}
$ecrRegistry = "$accountId.dkr.ecr.$awsRegion.amazonaws.com"
$ecrRepoUri = "$ecrRegistry/$appName"

Write-Host "[INFO] Deploying $appName to AWS in region $awsRegion..."
Write-Host "[INFO] App Title: $appTitle"
Write-Host "[INFO] Cluster: $ecsClusterName, Service: $ecsServiceName"

# Preflight checks
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "[ERROR] Docker CLI not found. Install Docker Desktop and ensure docker is on PATH."
}
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    throw "[ERROR] AWS CLI not found. Install AWS CLI v2 and run aws configure."
}
Invoke-Checked -Command "docker" -Arguments @("version")

# 1. Login to ECR
Write-Host "[INFO] Logging into ECR..."
$ecrPassword = (& aws ecr get-login-password --region $awsRegion)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($ecrPassword)) {
    throw "[ERROR] Failed to get ECR login password."
}
$ecrPassword | docker login --username AWS --password-stdin $ecrRegistry
if ($LASTEXITCODE -ne 0) {
    throw "[ERROR] Docker login to ECR failed."
}

# 2. Check/Create Repository
Write-Host "[INFO] Checking ECR Repository..."
& aws ecr describe-repositories --repository-names $appName --region $awsRegion | Out-Null
if ($LASTEXITCODE -ne 0) {
    Invoke-Checked -Command "aws" -Arguments @("ecr", "create-repository", "--repository-name", $appName, "--region", $awsRegion)
}

# 3. Build Docker Image
Write-Host "[INFO] Building Docker Image (Targeting linux/amd64 for Fargate)..."
$buildArgs = @(
    "build",
    "--platform", "linux/amd64",
    "--progress", "plain",
    "--build-arg", "VITE_APP_TITLE=$appTitle",
    "-f", "Dockerfile",
    "-t", "$appName`:latest",
    "."
)

if (-not [string]::IsNullOrWhiteSpace($env:VITE_COGNITO_USER_POOL_ID)) {
    $buildArgs += @("--build-arg", "VITE_COGNITO_USER_POOL_ID=$($env:VITE_COGNITO_USER_POOL_ID)")
}
if (-not [string]::IsNullOrWhiteSpace($env:VITE_COGNITO_CLIENT_ID)) {
    $buildArgs += @("--build-arg", "VITE_COGNITO_CLIENT_ID=$($env:VITE_COGNITO_CLIENT_ID)")
}

Invoke-Checked -Command "docker" -Arguments $buildArgs
Invoke-Checked -Command "docker" -Arguments @("tag", "$appName`:latest", "$ecrRepoUri`:latest")

# 4. Push to ECR
Write-Host "[INFO] Pushing to ECR (This may take a while)..."
Invoke-Checked -Command "docker" -Arguments @("push", "$ecrRepoUri`:latest")

# 5. Force ECS deployment and set desired count
Write-Host "[INFO] Updating ECS Service to pull new image..."
Invoke-Checked -Command "aws" -Arguments @("ecs", "update-service", "--cluster", $ecsClusterName, "--service", $ecsServiceName, "--force-new-deployment", "--region", $awsRegion)
Invoke-Checked -Command "aws" -Arguments @("ecs", "update-service", "--cluster", $ecsClusterName, "--service", $ecsServiceName, "--desired-count", $desiredCount, "--region", $awsRegion)

# 6. Retrieve URL
Write-Host "[INFO] Retrieving Application URL..."
$appUrl = ""
if (-not [string]::IsNullOrWhiteSpace($env:DOMAIN_NAME)) {
    $appUrl = "https://$($env:DOMAIN_NAME)"
}
else {
    $albName = "$appName-alb"
    try {
        $albDns = (aws elbv2 describe-load-balancers --names $albName --region $awsRegion --query "LoadBalancers[0].DNSName" --output text).Trim()
        if (-not [string]::IsNullOrWhiteSpace($albDns) -and $albDns -ne "None") {
            if (-not [string]::IsNullOrWhiteSpace($env:ACM_CERT_ARN)) {
                $appUrl = "https://$albDns"
            }
            else {
                $appUrl = "http://$albDns"
            }
        }
        else {
            $appUrl = "Unable to retrieve URL. Check AWS Console."
        }
    }
    catch {
        $appUrl = "Unable to retrieve URL. Check AWS Console."
    }
}

Write-Host "[SUCCESS] Deployment Artifact Pushed & Service Updated!"
Write-Host "Image URI: $ecrRepoUri`:latest"
Write-Host "Visit: $appUrl (Give it 2-3 mins to stabilize)"

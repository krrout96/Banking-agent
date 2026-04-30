# Terraform Backend Configuration
# This file configures the remote Terraform state backend.
# Update the values below to match the bucket and lock table used by GitHub Actions.

terraform {
  backend "s3" {
    bucket  = "amz-aidevops-470226123391-us-east-1-an"
    key     = "mig-dev/terraform.tfstate"
    region  = "us-east-1"
    encrypt = true
  }
}

# If you want to override the backend at init time, use:
# terraform init \
#   -backend-config="bucket=..." \
#   -backend-config="key=..." \
#   -backend-config="region=..." \
#   -backend-config="dynamodb_table=..."

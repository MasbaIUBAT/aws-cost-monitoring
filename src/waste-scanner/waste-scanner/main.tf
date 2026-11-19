# CloudSentry - Waste Scanner
#
# One `terraform apply` does all of this:
#   1. creates the Lambda execution role and attaches read-only permissions
#   2. zips scanner.py
#   3. deploys the Lambda with a 180-second timeout
#   4. invokes it once and prints the scan report as a Terraform output
#
# Nothing here can delete or modify AWS resources outside this stack.

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

# The region the Lambda itself lives in. The scan still covers every region.
provider "aws" {
  region = var.region
}

variable "region" {
  description = "Region to deploy the scanner Lambda into."
  type        = string
  default     = "ap-south-1"
}

variable "function_name" {
  description = "Name of the Lambda function."
  type        = string
  default     = "cost-scanner"
}

variable "timeout" {
  description = "Lambda timeout in seconds. A full multi-region sweep takes ~70s."
  type        = number
  default     = 180
}

# ---------------------------------------------------------------------------
# Package the function source
# ---------------------------------------------------------------------------

# Built at plan time, so there is no manual zip step and no artifact to commit.
data "archive_file" "code" {
  type        = "zip"
  source_file = "${path.module}/scanner.py"
  output_path = "${path.module}/scanner.zip"
}

# ---------------------------------------------------------------------------
# Execution role
# ---------------------------------------------------------------------------

resource "aws_iam_role" "scanner" {
  name = "${var.function_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Lets the function describe resources across all services.
# See README.md for the narrower least-privilege alternative.
resource "aws_iam_role_policy_attachment" "readonly" {
  role       = aws_iam_role.scanner.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# Lets the function write its own CloudWatch logs.
resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.scanner.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ---------------------------------------------------------------------------
# The function
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "scanner" {
  function_name = var.function_name
  role          = aws_iam_role.scanner.arn
  handler       = "scanner.lambda_handler"
  runtime       = "python3.13"
  timeout       = var.timeout

  filename = data.archive_file.code.output_path

  # Redeploys the code only when scanner.py actually changed.
  source_code_hash = data.archive_file.code.output_base64sha256

  # Without this the first invocation can race the policy attachment
  # and fail with UnauthorizedOperation.
  depends_on = [
    aws_iam_role_policy_attachment.readonly,
    aws_iam_role_policy_attachment.logs,
  ]
}

# ---------------------------------------------------------------------------
# Run it once and surface the report
# ---------------------------------------------------------------------------

data "aws_lambda_invocation" "run" {
  function_name = aws_lambda_function.scanner.function_name
  input         = "{}"
}

output "scan_report" {
  description = "Idle and orphaned resources found, grouped by region."
  value       = jsondecode(data.aws_lambda_invocation.run.result)
}

output "function_arn" {
  description = "ARN of the deployed scanner function."
  value       = aws_lambda_function.scanner.arn
}

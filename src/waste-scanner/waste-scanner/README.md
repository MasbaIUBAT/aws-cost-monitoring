# Waste Scanner — Multi-Region AWS Idle Resource Scanner

A serverless scanner that sweeps **every enabled AWS region** in the account and reports
resources that are running, orphaned, or idle — the ones that quietly generate a bill
every month because nobody remembers they exist.

Deployed entirely with **Terraform**. One `terraform apply` creates the IAM role, packages
the Python code, deploys the Lambda, invokes it once, and prints the scan report to the
terminal.

The function is **strictly read-only**. It cannot create, modify, or delete anything.

---

## What it detects

Per region:

| Check | Why it costs money |
|---|---|
| `running_ec2` | Billed per hour while running |
| `stopped_ec2` | Instance is free, but its attached EBS volumes still bill |
| `unattached_ebs` | Volume left behind after an instance was terminated — bills forever |
| `unused_elastic_ip` | An EIP not associated with anything is charged hourly |
| `old_snapshots` | EBS snapshots older than 90 days (configurable) |
| `amis` | Self-owned AMIs — each keeps a backing snapshot alive |
| `nat_gateways` | ~$30–40/month each, charged hourly whether used or not |
| `empty_load_balancers` | Load balancers with no healthy targets behind them |
| `rds` | Database instances and their current state |

Globally (once, not per region):

| Check | Notes |
|---|---|
| `s3_buckets` | S3 is a global namespace, so buckets are listed a single time |

Regions with nothing to report are omitted from the output, so the report stays short.

---

## Repository layout

```
src/waste-scanner/
├── README.md        # this file
├── scanner.py       # the Lambda function source
├── main.tf          # all Terraform infrastructure
└── .gitignore       # keeps Terraform state and build artifacts out of git
```

Everything for this component lives in this one directory. Run Terraform from here.

---

## Prerequisites

| Tool | Purpose |
|---|---|
| Terraform ≥ 1.6 | Creates the infrastructure |
| AWS CLI v2 | Supplies credentials to Terraform |
| An AWS account | With permission to create IAM roles and Lambda functions |

Python is **not** required on your machine. The code only ever runs inside Lambda, which
ships with Python 3.13 and `boto3` preinstalled. Terraform just zips the file and uploads it.

### Installing on Windows

In PowerShell:

```powershell
winget install Hashicorp.Terraform
winget install Amazon.AWSCLI
```

Close and reopen PowerShell so the new commands are on your PATH, then verify:

```powershell
terraform -version
aws --version
```

On macOS use `brew install terraform awscli`; on Linux use your package manager or
HashiCorp's apt/yum repository.

---

## Configuring AWS credentials

Terraform does not read the AWS Console — it authenticates with an access key pair.

1. In the AWS Console, open **IAM → Users → your user → Security credentials**.
2. Choose **Create access key**, and select the **Command Line Interface (CLI)** use case.
3. Copy **both** values. They are two different strings:
   - **Access Key ID** — starts with `AKIA`, 20 characters. Not secret.
   - **Secret Access Key** — roughly 40 characters. Shown **only once**, at creation time.
4. Run:

```powershell
aws configure
```

and supply, in order: the Access Key ID, the Secret Access Key, a default region
(e.g. `ap-south-1`), and `json` as the output format.

5. Confirm it works:

```powershell
aws sts get-caller-identity
```

A JSON response containing your account number and user ARN means the credentials are good.

> **Common failure:** `SignatureDoesNotMatch` almost always means the Secret Access Key is
> wrong — frequently because the Access Key ID was pasted into both prompts, or a trailing
> space was copied along with the key. The secret cannot be retrieved after creation; if it
> is lost, delete the key pair and create a new one.

> **Security:** never commit credentials, and never paste the Secret Access Key into a chat,
> an issue, or a code comment. Delete access keys you are no longer using.

---

## Deploying

From `src/waste-scanner/`:

```powershell
terraform init     # downloads the aws and archive providers
terraform apply    # shows a plan, then asks for confirmation
```

Type `yes` at the confirmation prompt. Terraform prints a plan first and acts only after
you approve it, which is what makes it safe to run against a live account.

A full run takes roughly 90 seconds: about 20 seconds to create the resources, then about
70 seconds for the scan itself across all regions.

To update the scanner later, edit `scanner.py` and run `terraform apply` again. Terraform
detects the changed source hash and redeploys only the Lambda.

To remove everything it created:

```powershell
terraform destroy
```

---

## What Terraform builds

`main.tf` declares five things:

1. **`provider "aws"`** — the region Terraform itself operates in. This is only where the
   Lambda *lives*; the scan still covers every region.

2. **`data "archive_file"`** — zips `scanner.py` into `scanner.zip` at plan time, so there
   is no manual packaging step and no zip file to commit.

3. **`aws_iam_role` + two policy attachments** — the function's execution role:
   - `ReadOnlyAccess` — the permission to describe resources across all services.
   - `AWSLambdaBasicExecutionRole` — the permission to write its own CloudWatch logs.

   The `depends_on` in the Lambda resource forces the policy attachment to complete first.
   Without it Terraform may create the function before its permissions exist, and the first
   invocation fails with `UnauthorizedOperation`.

4. **`aws_lambda_function`** — Python 3.13, 180-second timeout. The default Lambda timeout
   is 3 seconds, which is nowhere near enough: a full multi-region sweep takes over a
   minute and would be killed mid-scan.

   `source_code_hash` is what makes redeploys work. Terraform compares the hash of the
   zip against the deployed function and uploads new code only when the source changed.

5. **`data "aws_lambda_invocation"` + `output`** — invokes the function once immediately
   after deployment and prints the returned JSON as a Terraform output. This is why
   `terraform apply` ends with a scan report instead of just "Apply complete".

### Variables

Three values are parameterised, each with a working default, so nothing has to be edited
to deploy:

| Variable | Default | Purpose |
|---|---|---|
| `region` | `ap-south-1` | Where the Lambda itself is deployed |
| `function_name` | `cost-scanner` | Name of the function and its IAM role |
| `timeout` | `180` | Lambda timeout in seconds |

Override one at apply time:

```powershell
terraform apply -var="region=us-east-1"
```

---

## How the Python code works

**Region discovery.** `describe_regions()` is called once to list every region enabled on
the account, so no region list is hardcoded and newly enabled regions are picked up
automatically.

**Per-region clients.** AWS API endpoints are regional, so a fresh `boto3` client is built
for each region inside the loop. A client created outside the loop would only ever query
one region.

**Pagination.** EC2 APIs return results in pages. The code uses `get_paginator(...)` for
instances, volumes, and snapshots so that accounts with many resources are fully covered
rather than silently truncated at the first page.

**Per-region error isolation.** Each region's work is wrapped in `try/except`. If one
region fails — an opt-in region, a service not available there — the error is recorded
against that region and the sweep continues instead of aborting the entire scan.

**Empty-value filtering.** `check_region` returns only non-empty findings, and the handler
adds a region to the report only if something was found. A clean account produces a short
report rather than seventeen empty blocks.

**Read-only by construction.** Every API call is a `describe_*` or `list_*`. There is no
`delete`, `terminate`, `release`, or `modify` call anywhere in the file. The IAM role
enforces the same constraint independently, so even a mistaken code change could not
destroy anything.

---

## Sample output

```
scan_report = {
  "ap-northeast-2" = {
    "running_ec2" = [ "i-0xxxxxxxxxxxxxxxx" ]
  }
  "ap-southeast-1" = {
    "running_ec2" = [ "i-0xxxxxxxxxxxxxxxx" ]
  }
  "us-east-1" = {
    "amis" = [ "ami-0xxxxxxxxxxxxxxxx" ]
  }
  "s3_buckets (global)" = []
}
```

### Findings from the first real run

- An **unattached EBS volume** in `us-east-1`, left over from a terminated instance and
  billing monthly. Deleted.
- Two **running EC2 instances** in `ap-northeast-2` and `ap-southeast-1` — regions nobody
  was actively watching. Neither appears on the default console view, which opens on a
  single region.
- A **self-owned AMI** in `us-east-1`. An AMI itself is free, but it holds a backing
  snapshot that is billed for storage. Removing one properly takes two steps: deregister
  the AMI, then delete the snapshot it leaves behind.

---

## Interpreting the report

Not every line is waste:

- `unattached_ebs` and `unused_elastic_ip` are almost always genuine waste.
- `running_ec2` and `rds` are **informational** — they tell you what exists and where, but
  the decision about whether a workload is needed is yours.

Before deleting anything, check the resource's name and tags, and take a snapshot if the
data might matter. The scanner deliberately does not delete, so every removal stays a
deliberate human action.

---

## Notes on IAM

This uses the AWS-managed `ReadOnlyAccess` job-function policy. It is broad — among other
things it permits reading S3 object data — so it is not least-privilege.

A production version would attach a custom policy granting only the specific actions the
code calls:

```
ec2:DescribeRegions        ec2:DescribeNatGateways
ec2:DescribeInstances      elasticloadbalancing:DescribeLoadBalancers
ec2:DescribeVolumes        elasticloadbalancing:DescribeTargetGroups
ec2:DescribeAddresses      elasticloadbalancing:DescribeTargetHealth
ec2:DescribeSnapshots      rds:DescribeDBInstances
ec2:DescribeImages         s3:ListAllMyBuckets
                           sts:GetCallerIdentity
```

---

## Possible next steps

- Schedule it with EventBridge so it runs daily without manual invocation.
- Post the report to Slack, reusing the webhook pattern from the other components.
- Flag EC2 instances whose CloudWatch CPU has been near zero for 14 days, which identifies
  genuinely idle instances rather than merely running ones.
- Attach a least-privilege custom policy in place of `ReadOnlyAccess`.
- Deploy through CI so changes ship on push instead of from a laptop.

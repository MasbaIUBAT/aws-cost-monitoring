"""
CloudSentry - Waste Scanner.

Sweeps every enabled AWS region and reports resources that are running,
orphaned, or idle. Strictly read-only: every call is a describe_* or list_*.

Deployed by Terraform (see main.tf). Runs on Python 3.13, which ships with
boto3 preinstalled, so there are no dependencies to package.

IAM permissions required:
    ReadOnlyAccess (or the narrower custom policy listed in README.md)
"""

import boto3
from datetime import datetime, timezone, timedelta

# Snapshots older than this many days are reported.
OLD_DAYS = 90


def check_region(region, account_id):
    """Run every per-region check and return only the non-empty findings."""
    ec2 = boto3.client("ec2", region_name=region)
    elb = boto3.client("elbv2", region_name=region)
    rds = boto3.client("rds", region_name=region)
    cutoff = datetime.now(timezone.utc) - timedelta(days=OLD_DAYS)
    out = {}

    # EC2 instances, split by state. Stopped instances are free themselves,
    # but their attached EBS volumes keep billing.
    running, stopped = [], []
    for page in ec2.get_paginator("describe_instances").paginate():
        for r in page["Reservations"]:
            for i in r["Instances"]:
                state = i["State"]["Name"]
                if state == "running":
                    running.append(i["InstanceId"])
                elif state == "stopped":
                    stopped.append(i["InstanceId"])
    out["running_ec2"] = running
    out["stopped_ec2"] = stopped

    # EBS volumes in "available" state are attached to nothing and bill forever.
    out["unattached_ebs"] = [
        v["VolumeId"]
        for page in ec2.get_paginator("describe_volumes").paginate(
            Filters=[{"Name": "status", "Values": ["available"]}]
        )
        for v in page["Volumes"]
    ]

    # An Elastic IP with no AssociationId is not attached to anything.
    out["unused_elastic_ip"] = [
        a["PublicIp"]
        for a in ec2.describe_addresses()["Addresses"]
        if "AssociationId" not in a
    ]

    out["old_snapshots"] = [
        s["SnapshotId"]
        for page in ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"])
        for s in page["Snapshots"]
        if s["StartTime"] < cutoff
    ]

    # AMIs are free, but each holds a backing snapshot that is billed.
    out["amis"] = [img["ImageId"] for img in ec2.describe_images(Owners=["self"])["Images"]]

    # NAT Gateways bill hourly whether traffic flows through them or not.
    out["nat_gateways"] = [
        n["NatGatewayId"]
        for n in ec2.describe_nat_gateways(
            Filters=[{"Name": "state", "Values": ["available"]}]
        )["NatGateways"]
    ]

    # Load balancers with no registered targets behind them.
    empty_lbs = []
    for lb in elb.describe_load_balancers()["LoadBalancers"]:
        has_target = False
        for tg in elb.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"])["TargetGroups"]:
            if elb.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])["TargetHealthDescriptions"]:
                has_target = True
                break
        if not has_target:
            empty_lbs.append(lb["LoadBalancerName"])
    out["empty_load_balancers"] = empty_lbs

    out["rds"] = [
        f'{d["DBInstanceIdentifier"]} ({d["DBInstanceStatus"]})'
        for d in rds.describe_db_instances()["DBInstances"]
    ]

    # Drop empty lists so a clean region produces nothing at all.
    return {k: v for k, v in out.items() if v}


def lambda_handler(event, context):
    account_id = boto3.client("sts").get_caller_identity()["Account"]

    # Discover regions instead of hardcoding them, so newly enabled
    # regions are picked up automatically.
    regions = [
        r["RegionName"]
        for r in boto3.client("ec2", region_name="us-east-1").describe_regions()["Regions"]
    ]

    report = {}
    for region in regions:
        try:
            found = check_region(region, account_id)
            if found:
                report[region] = found
        except Exception as e:
            # One failing region must not abort the whole sweep.
            report[region] = {"error": str(e)}
        print(region, "done")

    # S3 is a global namespace, so buckets are listed once, not per region.
    try:
        s3 = boto3.client("s3")
        report["s3_buckets (global)"] = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    except Exception as e:
        report["s3_buckets (global)"] = {"error": str(e)}

    return report

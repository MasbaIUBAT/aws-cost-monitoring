"""
CloudSentry - Real-time alert when a billable resource is created.

Triggered by an EventBridge rule that listens to CloudTrail management
events. Reads who made the API call, what they created and where, then
posts it to Slack.

Billing data lags by up to 24 hours, so this is the layer that catches an
expensive resource at creation time instead of the next morning.

Environment variable required:
    SLACK_WEBHOOK_URL

IAM permissions required:
    none beyond the default CloudWatch Logs permissions
"""

import os
import json
import urllib.request

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Guard against a feedback loop: if this function's own role shows up as
# the caller, skip it instead of alerting on our own API calls.
SELF_MARKER = os.environ.get("SELF_MARKER", "cloudsentry")

# Rough monthly cost, shown so the reader knows whether to care.
COST_HINTS = {
    "CreateNatGateway": "~$32-43/month plus data processing",
    "RunInstances": "depends on instance type",
    "CreateDBInstance": "~$15-200/month",
    "AllocateAddress": "~$3.6/month even if unused",
    "CreateVolume": "~$0.08/GB/month",
    "CreateLoadBalancer": "~$16-22/month",
    "CreateVpcEndpoint": "~$7/month per AZ",
}


def _who(identity):
    """Turn the CloudTrail userIdentity block into a readable name."""
    if not identity:
        return "unknown"

    identity_type = identity.get("type")
    arn = identity.get("arn", "")

    if identity_type == "Root":
        return "ROOT USER (should not be used for daily work)"
    if identity_type == "IAMUser":
        return "{} (IAM user)".format(identity.get("userName") or arn)
    if identity_type == "AssumedRole":
        issuer = identity.get("sessionContext", {}).get("sessionIssuer", {})
        return "{} (assumed role)".format(issuer.get("userName") or arn)
    if identity_type == "AWSService":
        return "{} (AWS service)".format(identity.get("invokedBy", "service"))
    return arn or identity_type or "unknown"


def _dig(obj, *path):
    """Safely read a nested value out of dicts and lists."""
    current = obj
    for key in path:
        if current is None:
            return None
        if isinstance(key, int):
            if not isinstance(current, list) or len(current) <= key:
                return None
            current = current[key]
        else:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
    return current


def _resource_id(event_name, request, response):
    """Pull the resource identifier out of the event.

    Every AWS API puts it somewhere different, so each one is mapped
    explicitly. Anything not in the map falls through to a generic guess
    so an unknown event still produces a useful message.
    """
    lookups = {
        "RunInstances": lambda: _dig(response, "instancesSet", "items", 0, "instanceId"),
        "CreateNatGateway": lambda: _dig(
            response, "CreateNatGatewayResponse", "natGateway", "natGatewayId"
        ),
        "CreateVolume": lambda: _dig(response, "volumeId"),
        "AllocateAddress": lambda: _dig(response, "publicIp")
        or _dig(response, "allocationId"),
        "CreateDBInstance": lambda: _dig(response, "dBInstanceIdentifier"),
        "CreateDBCluster": lambda: _dig(response, "dBClusterIdentifier"),
        "CreateBucket": lambda: _dig(request, "bucketName"),
        "CreateFunction20150331": lambda: _dig(response, "functionName"),
        "CreateVpcEndpoint": lambda: _dig(
            response, "CreateVpcEndpointResponse", "vpcEndpoint", "vpcEndpointId"
        ),
        "CreateLoadBalancer": lambda: _dig(
            response, "loadBalancers", 0, "loadBalancerName"
        )
        or _dig(response, "loadBalancerName"),
        "CreateCluster": lambda: _dig(response, "cluster", "clusterName")
        or _dig(request, "name"),
    }

    getter = lookups.get(event_name)
    if getter:
        try:
            found = getter()
            if found:
                return str(found)
        except Exception:
            pass

    for blob in (response, request):
        if isinstance(blob, dict):
            for key, value in blob.items():
                if isinstance(value, str) and key.lower().endswith(("id", "name", "arn")):
                    return value

    return "(id not found)"


def _extra_detail(event_name, request, response):
    """A little more context for the events where size drives the cost."""
    if event_name == "RunInstances":
        instance_type = _dig(response, "instancesSet", "items", 0, "instanceType")
        count = len(_dig(response, "instancesSet", "items") or [])
        if instance_type:
            return "type {}, count {}".format(instance_type, count)
    if event_name == "CreateVolume":
        size = _dig(response, "size")
        volume_type = _dig(response, "volumeType")
        if size:
            return "{} GB, {}".format(size, volume_type or "")
    if event_name == "CreateDBInstance":
        instance_class = _dig(request, "dBInstanceClass")
        if instance_class:
            return "class {}".format(instance_class)
    return None


def build_message(detail):
    event_name = detail.get("eventName", "unknown")
    identity = detail.get("userIdentity", {})
    request = detail.get("requestParameters") or {}
    response = detail.get("responseElements") or {}

    who = _who(identity)
    resource = _resource_id(event_name, request, response)
    extra = _extra_detail(event_name, request, response)
    hint = COST_HINTS.get(event_name)

    lines = [
        "*New resource created*",
        "*Who:* {}".format(who),
        "*What:* {} -> `{}`".format(event_name, resource),
        "*Region:* {}".format(detail.get("awsRegion", "?")),
        "*When:* {}".format(detail.get("eventTime", "?")),
        "*Source IP:* {}".format(detail.get("sourceIPAddress", "?")),
    ]
    if extra:
        lines.insert(3, "*Details:* {}".format(extra))
    if hint:
        lines.append("*Estimated cost:* {}".format(hint))

    return "\n".join(lines)


def post_to_slack(text):
    if not SLACK_WEBHOOK_URL:
        raise RuntimeError("SLACK_WEBHOOK_URL is not set")

    payload = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.status


def lambda_handler(event, context):
    detail = event.get("detail") or {}

    # A failed API call did not create anything, so there is nothing to
    # warn about.
    if detail.get("errorCode") or detail.get("errorMessage"):
        print("skipped: failed API call -", detail.get("errorCode"))
        return {"statusCode": 200, "body": "skipped-error"}

    # Do not alert on our own calls.
    caller_arn = (detail.get("userIdentity") or {}).get("arn", "")
    if SELF_MARKER and SELF_MARKER in caller_arn:
        print("skipped: own call -", caller_arn)
        return {"statusCode": 200, "body": "skipped-self"}

    text = build_message(detail)
    print(text)
    post_to_slack(text)
    return {"statusCode": 200, "body": "sent"}

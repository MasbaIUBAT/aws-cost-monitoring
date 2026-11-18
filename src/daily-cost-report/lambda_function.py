"""
CloudSentry - Daily AWS cost report to Slack.

Runs once a day on an EventBridge schedule. Asks Cost Explorer what was
spent, then posts a summary to a Slack channel.

The important detail: every cost query filters on RECORD_TYPE = Usage.
Without that filter, promotional credits cancel out the usage and every
number comes back as $0.00, which hides real spend.

Environment variable required:
    SLACK_WEBHOOK_URL

IAM permissions required:
    ce:GetCostAndUsage
    ce:GetCostForecast
"""

import os
import json
import calendar
import urllib.request
from datetime import date, timedelta

import boto3
from botocore.exceptions import ClientError

# Cost Explorer only has an endpoint in us-east-1, whatever region
# this function itself runs in.
CE_REGION = "us-east-1"

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Anything smaller than this is noise, so it is left out of the report.
SERVICE_THRESHOLD = 0.01
USAGE_TYPE_THRESHOLD = 0.01
TOP_N_SERVICES = 6
TOP_N_USAGE_TYPES = 4

# "Charge Type = Usage" in the console is called RECORD_TYPE in the API.
# Reserved Instance and Savings Plan usage is real usage too, so it is
# included here.
USAGE_RECORD_TYPES = ["Usage", "DiscountedUsage", "SavingsPlanCoveredUsage"]
CREDIT_RECORD_TYPES = ["Credit", "Refund"]

ce = boto3.client("ce", region_name=CE_REGION)


def _money(value):
    """Round to cents, and turn tiny negatives like -0.0000037 into 0.00.

    Without this the report prints "$-0.00", which looks like a bug.
    """
    if value is None:
        return None
    return 0.0 if abs(value) < 0.005 else round(value, 2)


def _fmt(value):
    if value is None:
        return "not available"
    return "${:,.2f}".format(_money(value))


def _record_type_filter(values):
    return {"Dimensions": {"Key": "RECORD_TYPE", "Values": values}}


def get_cost(start, end, record_types=None):
    """Total cost for [start, end). The end date is exclusive."""
    if start >= end:
        return 0.0

    record_types = record_types or USAGE_RECORD_TYPES
    kwargs = {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "Filter": _record_type_filter(record_types),
    }

    total = 0.0
    next_token = None
    while True:
        if next_token:
            kwargs["NextPageToken"] = next_token
        response = ce.get_cost_and_usage(**kwargs)
        for bucket in response["ResultsByTime"]:
            total += float(bucket["Total"]["UnblendedCost"]["Amount"])
        next_token = response.get("NextPageToken")
        if not next_token:
            break
    return total


def get_breakdown(start, end):
    """Cost grouped by service AND usage type in a single API call.

    Cost Explorer accepts up to two GroupBy dimensions per request. Asking
    for both at once avoids one extra call per service, and each Cost
    Explorer API call is billed at $0.01.

    Returns (total, {service: {"total": x, "usage_types": {name: cost}}})
    """
    if start >= end:
        return 0.0, {}

    kwargs = {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "Filter": _record_type_filter(USAGE_RECORD_TYPES),
        "GroupBy": [
            {"Type": "DIMENSION", "Key": "SERVICE"},
            {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
        ],
    }

    total = 0.0
    services = {}
    next_token = None

    while True:
        if next_token:
            kwargs["NextPageToken"] = next_token
        response = ce.get_cost_and_usage(**kwargs)

        for bucket in response["ResultsByTime"]:
            for group in bucket.get("Groups", []):
                service, usage_type = group["Keys"][0], group["Keys"][1]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])

                entry = services.setdefault(
                    service, {"total": 0.0, "usage_types": {}}
                )
                entry["total"] += amount
                entry["usage_types"][usage_type] = (
                    entry["usage_types"].get(usage_type, 0.0) + amount
                )
                total += amount

        next_token = response.get("NextPageToken")
        if not next_token:
            break

    return total, services


def get_forecast(start, end):
    """Forecast spend for the rest of the month.

    The same Usage filter is applied here. If it is left off, the forecast
    is calculated on a different basis than the actual numbers above it,
    and the two disagree.

    A brand new account may not have enough history for AWS to forecast at
    all, so this returns None instead of failing the whole report.
    """
    if start >= end:
        return 0.0

    base = {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
        "Metric": "UNBLENDED_COST",
        "Granularity": "DAILY",
    }

    try:
        response = ce.get_cost_forecast(
            Filter=_record_type_filter(USAGE_RECORD_TYPES), **base
        )
        return float(response["Total"]["Amount"])
    except ClientError:
        pass

    try:
        response = ce.get_cost_forecast(**base)
        return float(response["Total"]["Amount"])
    except ClientError:
        return None


def build_report(today=None):
    today = today or date.today()
    yesterday = today - timedelta(days=1)

    first_of_month = today.replace(day=1)
    last_day_num = calendar.monthrange(today.year, today.month)[1]
    first_of_next_month = date(today.year, today.month, last_day_num) + timedelta(days=1)
    first_of_last_month = (first_of_month - timedelta(days=1)).replace(day=1)

    yesterday_total, services = get_breakdown(yesterday, today)
    month_to_date = get_cost(first_of_month, today)
    last_month = get_cost(first_of_last_month, first_of_month)
    credits_applied = get_cost(
        first_of_month, today, record_types=CREDIT_RECORD_TYPES
    )

    forecast_rest = get_forecast(today, first_of_next_month)
    month_end = None if forecast_rest is None else month_to_date + forecast_rest

    lines = [
        "*AWS Cost Report - {}*".format(yesterday.isoformat()),
        "Yesterday: {}".format(_fmt(yesterday_total)),
        "Month to date: {}".format(_fmt(month_to_date)),
        "Month-end forecast: {}".format(_fmt(month_end)),
        "Last month total: {}".format(_fmt(last_month)),
    ]

    # Credits come back as negative numbers, so flip the sign for display.
    credit_amount = abs(_money(credits_applied) or 0.0)
    if credit_amount > 0:
        lines.append("Credits applied this month: -{}".format(_fmt(credit_amount)))
        lines.append("_Figures above are real usage, before credits._")

    lines.append("")
    lines.append("*By service and usage type (yesterday):*")

    top_services = [
        (name, data)
        for name, data in sorted(
            services.items(), key=lambda kv: kv[1]["total"], reverse=True
        )
        if data["total"] >= SERVICE_THRESHOLD
    ][:TOP_N_SERVICES]

    if not top_services:
        lines.append("- No significant spend")
        return "\n".join(lines)

    for name, data in top_services:
        lines.append("- *{}* - {}".format(name, _fmt(data["total"])))

        top_types = [
            (usage_type, amount)
            for usage_type, amount in sorted(
                data["usage_types"].items(), key=lambda kv: kv[1], reverse=True
            )
            if amount >= USAGE_TYPE_THRESHOLD
        ][:TOP_N_USAGE_TYPES]

        for usage_type, amount in top_types:
            lines.append("        - {} - {}".format(usage_type, _fmt(amount)))

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
    text = build_report()
    print(text)
    post_to_slack(text)
    return {"statusCode": 200, "body": "sent"}

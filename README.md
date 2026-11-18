CloudSentry

Cost visibility and resource alerts for AWS, built with Lambda, Cost Explorer, CloudTrail and EventBridge.

CloudSentry answers two questions in a Slack channel:

What did we spend yesterday, and on what? A daily report, broken down by service and by the exact resource type inside that service.
Who just created something that will cost money? A real-time alert, usually within a minute of the resource being created.

Everything in this repository was built by hand in the AWS Console first, with no Terraform. The setup guide below is the exact sequence, so anyone can rebuild it from scratch in about 45 minutes.

Why I built it

I opened my AWS billing page and it said I had spent $0.00. Nothing yesterday, nothing this month, nothing last month. But the month-end forecast said $31.11.

Those two things cannot both be true. Either I was spending nothing, in which case the forecast should also be near zero, or I was spending something and the report was lying to me.

It turned out the report was lying to me, and it was my own code doing it.

My account had promotional credits. AWS was charging me for usage, then immediately applying credits to cancel it out. Net payable: zero. Actual consumption: about $34 a month. My script was asking Cost Explorer for the total cost without filtering anything out, so usage and credits added up to nothing.

The bug was hidden in plain sight. The report printed $-0.00 instead of $0.00. That minus sign is the giveaway: a true zero has no sign. It meant the real number was a tiny negative, which is what you get when credits very slightly exceed usage after rounding.

Fixing that one filter turned a report saying "$0, no significant spend" into a report saying "$34.52 this month, $65.63 projected, and here is exactly which resources are eating it."

That is the whole point of this project. A $0 bill is not the same thing as $0 of spend. When the credits run out, the real number is what you pay.

What it does
Part 1: Daily cost report

Runs once a day. Posts something like this:

AWS Cost Report - 2026-11-17
Yesterday: $2.14
Month to date: $34.52
Month-end forecast: $65.63
Last month total: $29.65
Credits applied this month: -$34.52
Figures above are real usage, before credits.

By service and usage type (yesterday):
- EC2 - Other - $0.92
        - APS1-EBS:VolumeUsage.gp3 - $0.71
        - APS1-EBS:SnapshotUsage - $0.21
- Amazon Elastic Compute Cloud - Compute - $0.87
        - APS1-BoxUsage:t3.small - $0.87
- Amazon Virtual Private Cloud - $0.35
        - APS1-PublicIPv4:InUseAddress - $0.35

The credits line is the part that matters. It separates "what you pay today" from "what you will pay when the free money is gone."

Part 2: Real-time resource alert

Fires when someone creates a billable resource. Posts something like this:

New resource created
Who: Practice_arafat (IAM user)
What: RunInstances -> i-0870a59ab5496cef8
Details: type t3.micro, count 1
Region: ap-southeast-1
When: 2026-11-18T15:40:43Z
Source IP: 203.0.113.10
Estimated cost: depends on instance type

In my testing the Slack message arrived in under a minute from the instance launching.

Screenshots

Daily cost report in Slack. The credits line is the one that matters: it separates what the invoice says from what the account actually consumed. Under each service is the usage-type breakdown, which is what turns "EC2 - Other" into something you can act on.

Daily cost report and resource alert in Slack

EventBridge rule. The event pattern filters the CloudTrail stream down to the API calls that create billable resources, and routes them to the alert Lambda.

EventBridge rule

CloudTrail trail. Required for EventBridge to receive any CloudTrail events. The default Event history page is not a trail and does not work for this.

CloudTrail trail

IAM policy. Least privilege: the report function gets read-only Cost Explorer access and nothing else.

IAM policy

End-to-end test. The t3.micro launched to confirm the alert fires from a real resource, not just a hand-run test event.

EC2 test instance

Architecture
PART 1 - Scheduled reporting (pull)

   EventBridge Scheduler (daily)
            |
            v
   Lambda: cloudsentry-daily-cost
            |
            v
   Cost Explorer API  ---->  Slack webhook


PART 2 - Event-driven alerting (push)

   Someone calls RunInstances / CreateNatGateway / ...
            |
            v
   CloudTrail (trail must exist, see below)
            |
            v
   EventBridge default bus
            |
            v
   EventBridge rule: cloudsentry-resource-created
            |
            v
   Lambda: cloudsentry-resource-alerts  ---->  Slack webhook
Why two systems instead of one

This is the design decision behind the whole project.

Cost Explorer data is not real time. AWS refreshes billing data roughly once every 24 hours. If someone launches an expensive instance at 9 AM, the cost report will not show it until the next morning. By then you have already paid for a full day.

CloudTrail is real time, but has no dollar amounts. It records who called which API, with what parameters, from which IP. It knows nothing about money.

So the two layers do different jobs:

	Answers	Latency
CloudTrail alert	Who created what	Under a minute
Cost Explorer report	What it actually cost	Next day

Detection is real-time. Attribution is next-day. Neither one on its own is enough.

Problems I ran into, and how I fixed them

This section is the honest version. Each of these cost me time, and each one taught me something.

1. Every number was $0.00 because of credits

Symptom: Daily cost, month to date and last month all showed $-0.00, but the forecast showed $31.11.

Cause: My Cost Explorer query had no filter. Credits are returned as negative line items, so usage of +$34.52 and credits of -$34.52 summed to approximately zero. The report was technically correct and completely useless.

Fix: Filter on RECORD_TYPE = Usage in every cost query. In the AWS Console this same filter is labelled "Charge Type", which is why it is easy to miss when you move from the console to the API.

python
"Filter": {
    "Dimensions": {
        "Key": "RECORD_TYPE",
        "Values": ["Usage", "DiscountedUsage", "SavingsPlanCoveredUsage"]
    }
}

DiscountedUsage and SavingsPlanCoveredUsage are included because Reserved Instance and Savings Plan consumption is still real usage. Leaving them out would under-report any account that uses commitments.

I also added a separate query for Credit and Refund so the report shows how much credit is being burned, as its own line. That is the number that tells you how long you have left.

2. Forecast and actuals disagreed with each other

Symptom: Actual spend showed $0, forecast showed $31.11.

Cause: I had fixed the filter on get_cost_and_usage but not on get_cost_forecast. The two calls were measuring different things.

Fix: Apply the same filter to both. Also wrapped the forecast call in a try/except, because a new account may not have enough history for AWS to forecast at all. A missing forecast should not take down the whole report.

3. $-0.00 in the output

Symptom: Cosmetic, but it made people think the code was broken.

Cause: Floating point values like -0.0000037 round to -0.00 at two decimal places.

Fix: Treat anything under half a cent as exactly zero before formatting.

python
def _money(value):
    return 0.0 if abs(value) < 0.005 else round(value, 2)
4. EventBridge never fired, because CloudTrail Event History is not a trail

Symptom: The EventBridge rule was created and enabled, the Lambda worked when tested by hand, but nothing happened when I actually created a resource.

Cause: This one is not obvious. The Event history page in the CloudTrail console is on by default and free, and it shows every API call. It is easy to assume that is enough. It is not. To receive events with a detail-type of AWS API Call via CloudTrail, the account needs an actual trail that is logging management events. Event history does not count.

Fix: Create a trail. The first copy of management events is delivered free, so there is no CloudTrail charge, only a few cents of S3 storage.

Think of it like CCTV. The monitor is on and you can see the picture, but unless recording is switched on, nothing is written anywhere for another system to read.

5. I launched an instance and no alert came, because I was in the wrong region

Symptom: Created a t3.micro, waited, nothing in Slack.

Cause: I had created the instance in Mumbai (ap-south-1). My Lambda and EventBridge rule were in Singapore (ap-southeast-1). EventBridge is a regional service. A CloudTrail event is delivered to the event bus in the region where the API call happened. Mumbai events go to the Mumbai bus, and my Singapore rule never saw them.

Fix for the demo: Create the resource in the same region as the rule.

Fix for production: See the Known Limitations section below.

This is a limitation worth understanding rather than hiding. It is also the reason a multi-region CloudTrail trail is not enough on its own: the trail being multi-region controls where logs are stored, not which event bus receives the event.

6. Avoiding a feedback loop

Risk: The alerting Lambda will eventually make its own AWS API calls, for example to tag a resource. Those calls are logged by CloudTrail too. With a broad enough rule, the function would alert on its own activity, trigger itself again, and keep going.

Fix: Two guards.

The event pattern lists specific eventName values rather than matching everything.
The function checks whether the calling identity is its own role and returns early if so.
7. Cost Explorer API calls are not free

Detail that surprised me: Each Cost Explorer API request costs $0.01.

My first idea for the usage-type breakdown was to loop over the top services and make one extra API call per service. With 3 services that is 3 extra calls a day, which is about $1 a month on top of the 5 calls the report already makes. On an account spending $65 a month, that is not nothing.

Fix: Cost Explorer accepts up to two GroupBy dimensions in a single request. Asking for SERVICE and USAGE_TYPE together gives the full breakdown in one call instead of several.

python
"GroupBy": [
    {"Type": "DIMENSION", "Key": "SERVICE"},
    {"Type": "DIMENSION", "Key": "USAGE_TYPE"}
]

Total cost of running the whole system is now roughly $1.50 to $2 a month, almost all of it Cost Explorer API calls.

8. "EC2 - Other" hides the things that actually cost money

Not a bug, but worth writing down because it is where most surprise bills live.

Amazon Elastic Compute Cloud - Compute is only the instance hours: CPU and memory. Everything attached to the instance is billed under a separate line called EC2 - Other, which includes:

EBS volumes (these survive after an instance is terminated, if you are not careful)
EBS snapshots
NAT Gateway hours and data processing
some data transfer

So a server that shows as $200 under "Compute" may actually be costing $290 once "EC2 - Other" is counted. Grouping by USAGE_TYPE is what breaks that open and shows which specific thing it is.

A related one: Amazon Virtual Private Cloud charges are usually public IPv4 addresses. Since 2024 AWS charges about $0.005 per hour for every public IPv4 address, whether or not it is attached to anything. A forgotten Elastic IP is roughly $3.60 a month doing nothing.

Setup guide, no Terraform

Everything below is done in the AWS Console. Takes about 45 minutes.

Before you start
An AWS account with admin access
A Slack workspace where you can create an incoming webhook
Pick one region and stay in it. This guide uses ap-southeast-1 (Singapore). Whichever you pick, every resource below must be in the same one.
Step 0: Create the Slack webhook
Go to https://api.slack.com/apps and click Create New App → From scratch
Name it CloudSentry, pick your workspace
Open Incoming Webhooks, turn it on
Add New Webhook to Workspace, choose the channel, click Allow
Copy the webhook URL

Treat this URL as a password. Anyone who has it can post messages into your channel. It goes into a Lambda environment variable, never into the source code and never into git.

Step 1: Enable Cost Explorer
Go to Billing and Cost Management → Cost Explorer
If this is the first time, click Enable Cost Explorer

It can take up to 24 hours before data is available on a brand new account.

Step 2: Create the daily cost report Lambda
Lambda → Create function → Author from scratch
Function name: cloudsentry-daily-cost
Runtime: Python 3.13 or newer
Click Create function
Paste the contents of src/daily-cost-report/lambda_function.py into the editor, replacing everything
Click Deploy

Then configure it:

Configuration → Environment variables → Edit → Add
Key: SLACK_WEBHOOK_URL
Value: your webhook URL
Configuration → General configuration → Edit
Timeout: 1 minute (the default of 3 seconds is nowhere near enough; this function makes five Cost Explorer calls)
Step 3: Give that Lambda permission to read billing data
Configuration → Permissions → click the Role name link
In IAM, Add permissions → Create inline policy → JSON
Paste policies/cost-explorer-read-policy.json
Name it CostExplorerReadAccess → Create policy

Cost Explorer does not support resource-level permissions, so "Resource": "*" is required here. It is read-only.

Step 4: Test the daily report
Back in Lambda, Test tab → Create new event
Name: manual-run, body: {}
Save → Test

You should see "statusCode": 200 and the full report printed in the execution log, plus a message in Slack.

If you get AccessDeniedException, wait ten seconds for the IAM policy to propagate and try again.

Step 5: Schedule the daily report
On the function page, Add trigger → EventBridge (CloudWatch Events)
Create a new rule
Name: cloudsentry-daily-schedule
Rule type: Schedule expression
Expression: cron(0 3 * * ? *) — this is 3 AM UTC, which is 9 AM in Dhaka. Pick a time a few hours after midnight UTC so yesterday's data has settled.
Add
Step 6: Create a CloudTrail trail

This step is required and easy to skip. Without a trail, EventBridge receives no CloudTrail events at all, and Part 2 will silently do nothing.

CloudTrail → Trails → Create trail
Trail name: cloudsentry-trail
Leave the S3 bucket as the one AWS offers to create
Management events: Write only (Read events are high volume and not needed here)
Data events: leave off (this is where CloudTrail bills can get expensive)
Create trail

Confirm the Status column says Logging.

Optional but recommended: open the S3 bucket it created, go to Management → Create lifecycle rule, and expire objects after 30 days. Otherwise log files accumulate forever, which is exactly the kind of quiet growth this project is meant to catch.

Step 7: Create the resource alert Lambda
Lambda → Create function → Author from scratch
Function name: cloudsentry-resource-alerts
Runtime: Python 3.13 or newer
Create function
Paste src/resource-alerts/lambda_function.py, then Deploy
Configuration → Environment variables → add SLACK_WEBHOOK_URL
Configuration → General configuration → Timeout: 30 seconds

This function needs no extra IAM permissions. It only reads the event it is handed and makes one outbound HTTPS call.

Step 8: Test the alert Lambda by hand
Test tab → Create new event
Name: nat-sample
Body: paste policies/sample-test-event.json
Save → Test

A message should appear in Slack. Note that the sample event uses obviously fake values (nat-SAMPLE-NOT-REAL, SAMPLE-USER) so nobody mistakes the test for a real resource.

This only proves the Lambda and the Slack connection work. It does not prove the wiring works, because you invoked the function yourself. That is the next step.

Step 9: Create the EventBridge rule
EventBridge → Event buses → click default → Rules tab → Create rule
In the newer console, rules live inside the bus rather than in the left menu
If you see a Builder mode choice at the top, pick Advanced builder for the step-by-step flow
Name: cloudsentry-resource-created, Event bus: default, Rule type: Rule with an event pattern → Next
Event source: Other
Creation method: Custom pattern (JSON editor)
Paste policies/eventbridge-rule-pattern.json, confirm it says JSON is valid → Next
Target types: AWS service → Select a target: Lambda function → Function: cloudsentry-resource-alerts
Execution role: leave on Create default role
Next → Next → Create rule

EventBridge adds the permission for itself to invoke your Lambda automatically.

Step 10: End-to-end test
Make sure the console region matches the rule's region
EC2 → Launch instance → pick t3.micro → launch
Watch Slack

The alert should arrive within a minute or two. Then:

Terminate the instance
EC2 → Volumes and confirm the volume was deleted with it

Step 5 matters. Terminating an instance does not always delete its EBS volume, and an orphaned volume keeps billing under "EC2 - Other" forever.

Known limitations
1. Alerts only cover one region

EventBridge is regional. A CloudTrail event is delivered to the event bus in the region where the API call was made. The rule in this guide lives in ap-southeast-1, so creating a resource in ap-south-1 or us-east-1 produces no alert.

I hit this during testing: I launched an instance in Mumbai while my rule was in Singapore, and nothing arrived. The system was working correctly; I was testing in the wrong place.

How to solve it properly: put a small forwarding rule in every region you use, each one targeting the central event bus in your primary region. That pattern is called cross-region event forwarding. One Lambda then handles every region.

Also note: global services such as IAM log their events to us-east-1 regardless of where you are working, so a rule covering IAM activity has to live there.

2. Cost data is up to 24 hours behind

Not fixable. It is how AWS billing works. It is the reason Part 2 exists.

3. No per-user cost attribution yet

The alert says who created a resource. The cost report says what was spent. They are not joined.

Joining them needs tagging:

Tag resources with Owner and Project
Activate those as cost allocation tags in the Billing console
Group the Cost Explorer query by TAG instead of SERVICE

The natural next step is to have the alerting Lambda apply an Owner tag automatically, using the identity it already reads from the CloudTrail event. That removes the "people forget to tag" problem, which is what usually kills tagging strategies.

Two things to know before relying on it: cost allocation tags are not retroactive, so costs incurred before you activate a tag can never be attributed, and tag data takes about 24 hours to appear in billing data.

4. No resource-level cost

The report goes down to usage type (EBS:VolumeUsage.gp3), not to the individual volume ID. For per-resource cost there are two options:

Enable resource-level data in Cost Explorer preferences. Covers the last 14 days only, and is billed per usage record.
Export CUR 2.0 via Data Exports to S3 and query it with Athena. No 14-day limit, full history, and it is the standard approach for serious cost analysis.
5. Nothing is in version control as infrastructure

Everything here was clicked together by hand. That was deliberate: building it manually first is how I learned which pieces depend on each other, and why. The Terraform version is the next step, and it will reproduce the same stack with one command.

Running cost
Item	Monthly
Cost Explorer API, ~6 calls/day at $0.01	~$1.80
Lambda invocations	effectively $0 (free tier)
CloudTrail management events, first copy	$0
S3 storage for trail logs	a few cents
Total	about $2

Worth saying plainly: a cost monitoring tool that costs $2 a month to find $40 a month of waste pays for itself many times over. But it is not free, and the Cost Explorer API pricing is the reason the code batches its queries instead of making one call per service.

What I would tell an interviewer about this

The short version:

Billing data lags by up to 24 hours, so a daily cost report alone always tells you about waste after you have already paid for it. I built two layers. CloudTrail and EventBridge catch a resource at creation time and post who made it to Slack within a minute. A scheduled Lambda queries Cost Explorer the next day and confirms what it actually cost, with credits broken out separately so the real run rate is visible even while promotional credits make the invoice read $0.

The thing I would actually want to talk about is the credits bug. The report said zero, the forecast said thirty-one dollars, and the two could not both be right. Chasing that down is what taught me the difference between what you pay and what you consume, and that difference is the whole job in cloud cost management.

Repository layout
cloudsentry/
├── README.md
├── LICENSE
├── .gitignore
├── src/
│   ├── daily-cost-report/
│   │   └── lambda_function.py      Daily Cost Explorer report
│   └── resource-alerts/
│       └── lambda_function.py      CloudTrail-driven alerts
├── policies/
│   ├── cost-explorer-read-policy.json
│   ├── eventbridge-rule-pattern.json
│   └── sample-test-event.json
└── screenshots/
Security notes
The Slack webhook URL is a secret. It lives in a Lambda environment variable, never in the code and never in git. .gitignore covers the usual places it could leak.
For a production account, move it to AWS Secrets Manager or SSM Parameter Store and read it at runtime.
Both IAM policies are least privilege: the report function gets read-only Cost Explorer access, and the alert function gets nothing beyond writing its own logs.
The alert function treats a root user action as notable and labels it clearly in the message. Root should not be used for day-to-day work, so an alert from root is worth looking at.
Roadmap
[ ] Terraform module for the whole stack
[ ] Auto-tagging: apply Owner from the CloudTrail identity, then report cost grouped by tag
[ ] Cross-region event forwarding to a central bus
[ ] AWS Budgets alert and Cost Anomaly Detection wired into the same Slack channel
[ ] Weekly top-20 most expensive resources, via CUR 2.0 and Athena
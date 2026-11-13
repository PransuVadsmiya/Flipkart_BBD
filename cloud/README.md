# AWS deployment (faculty deliverables for Theme 1: Scalability & High-Traffic Events)

Faculty steps mapped to this repo:

| Faculty step | Here |
|---|---|
| 1. Basic web API returning a status | `app/main.py`, `GET /status` returns `{"status": "Big Billion Days deal available", ...}` |
| 2. Containerize / VM | Docker container started on each EC2 instance by `cloud/aws/user_data.sh.tftpl` |
| 3. Load balancer | Application Load Balancer (`aws_lb`), health check on `/health` |
| 4. Auto Scaling Group, scale out above 50% CPU | `aws_autoscaling_group` + target-tracking policy on average CPU = 50% (min 1, max 4) |
| 5. Load test | Locust, 5x spike (16 to 80 req/s, about 50k requests), `cloud/run_aws_test.py` |
| 6. Evidence | CloudWatch dashboard `bbd-scaling`, ASG activity log, `results/aws/` plots |

In AWS mode the app runs with `WORK_MODE=cpu`: each request burns real CPU (about 46 ms for the shopper mix), so CPU
utilisation tracks load and the 50% rule fires. One t3.small (2 vCPU) saturates near 43 req/s (about 22 req/s at the 50% target), so 80 req/s needs about 4 instances.
The max of 4 comes from the account's default limit of 8 vCPUs (4 x 2 vCPU); a first run at 200 req/s asked for 8 instances and AWS refused the extra four with
"vCPU limit of 8". That is worth a sentence in the report (service quotas are a real constraint on scale-out).
Instances keep their cache and stock in memory (`REDIS_URL=memory`); a shared cache (ElastiCache) would be the next step
and is discussed in the report, not built.

## Prerequisites (once)

1. An AWS account with permission to create EC2, ELB, Auto Scaling, CloudWatch and IAM-free resources (an admin/PowerUser IAM user is simplest).
2. `winget install Amazon.AWSCLI` and `winget install Hashicorp.Terraform` (open a new terminal afterwards).
3. `aws configure` (access key, secret, region `ap-south-1`, output `json`), then `aws sts get-caller-identity` must print your account.
4. `pip install boto3 locust matplotlib pandas` in the project venv.

## Deploy

```
cd cloud\aws
terraform init
terraform validate
terraform plan
terraform apply
```

Type `yes`. It takes about 3 to 5 minutes. Outputs: `live_url`, `alb_dns`, `asg_name`, `dashboard`.
Wait 2 to 3 minutes more (instances boot, install Docker, start the app), then open `live_url` in a browser: it must show the
JSON status. Refresh a few times: the `instance` field changes between instances, which shows load balancing.

## Run the test (about 40 minutes)

From the repo root (`C:\Cloud_Demo`):

```
python cloud\run_aws_test.py --url http://<alb_dns>
```

Leave the AWS console open while it runs and take the screenshots below. When it ends it prints a summary and writes
`results\aws\` (timeline.png, latency_vs_instances.png, summary.json, scaling_activities.txt, Locust CSVs).

## Screenshots to take (the faculty deliverables)

All times are within the 40-minute run. Include the browser URL bar and the time/clock where possible.

1. **Live URL**: browser on `http://<alb_dns>/status` showing the JSON (before the test, and once during).
2. **ASG overview**: EC2 > Auto Scaling groups > `bbd-asg`: before the test (desired 1) and at peak (desired 3 to 4).
3. **ASG "Activity" tab**: shows "Launching a new EC2 instance" entries with the cause "TargetTracking ... AlarmHigh". Take it at peak and again after scale-in ("Terminating EC2 instance").
4. **ASG "Instance management" tab** at peak: list of InService instances.
5. **ASG "Monitoring" tab** or the **CloudWatch dashboard `bbd-scaling`** after the run: instances, CPU against the 50% line, ALB response time, requests per minute. The dashboard is the AWS-native version of "response time vs instance count".
6. **EC2 Instances list** at peak, filtered by name `bbd-app`.
7. **Load balancer page** (EC2 > Load balancers > `bbd-alb` > Target groups): healthy target count at peak.

Also keep `results\aws\latency_vs_instances.png` (the required plot) and `results\aws\scaling_activities.txt` (the log evidence).

## Cost and teardown

A t3.small is about USD 0.02 per hour and the ALB about USD 0.03 per hour, so a 40-minute test with up to 12 instances costs well under USD 1
(check the AWS price list for ap-south-1; pass `--price-per-hour` if it differs). Do not leave it running: 2 instances + ALB is roughly USD 50 per month.
When you have your screenshots, and after your faculty has checked the live URL if they ask for it to stay up:

```
cd cloud\aws
terraform destroy
```

If the run fails halfway, `terraform destroy` still removes everything.

## Troubleshooting

* `live_url` returns 502/503 for the first few minutes: normal while instances boot. Check target health in the ALB console. If it stays unhealthy,
  open EC2 > instance > Monitor and troubleshoot > Get system log, and look for errors from the user-data script (log file `/var/log/user-data.log`).
* `terraform apply` says the default VPC does not exist: create one with `aws ec2 create-default-vpc`.
* Scale-out is slower than locally: that is expected (1-minute metrics, alarm needs several datapoints, about 2 minutes to boot). Scale-in starts about 15 minutes after the load drops.

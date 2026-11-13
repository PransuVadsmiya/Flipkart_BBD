"""Poll the Auto Scaling Group during a load test and record instance counts + CPU.

Writes a timeline.csv in the same schema as the local autoscaler, so experiments/analyze.py can plot
latency against instance count. At the end it also dumps the ASG scaling activity log (evidence that the
scale-out/in was automatic).

Needs AWS credentials (`aws configure`) and `pip install boto3`.
"""
import argparse
import csv
import datetime as dt
import os
import time

import boto3


def snapshot(asg_c, elb_c, cw_c, asg_name):
    d = asg_c.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])["AutoScalingGroups"][0]
    insts = d["Instances"]
    launched = len(insts)                                   # pending + in service (+ terminating): billed
    in_service = sum(1 for i in insts if i["LifecycleState"] == "InService")
    healthy = in_service
    arns = d.get("TargetGroupARNs") or []
    if arns:
        try:
            th = elb_c.describe_target_health(TargetGroupArn=arns[0])["TargetHealthDescriptions"]
            healthy = sum(1 for t in th if t["TargetHealth"]["State"] == "healthy")   # actually serving
        except Exception:
            pass
    now = dt.datetime.now(dt.timezone.utc)
    cpu = None
    try:
        pts = cw_c.get_metric_statistics(
            Namespace="AWS/EC2", MetricName="CPUUtilization",
            Dimensions=[{"Name": "AutoScalingGroupName", "Value": asg_name}],
            StartTime=now - dt.timedelta(minutes=5), EndTime=now, Period=60, Statistics=["Average"])["Datapoints"]
        if pts:
            cpu = max(pts, key=lambda p: p["Timestamp"])["Average"]
    except Exception:
        pass
    return {"launched": launched, "ready": healthy, "in_service": in_service,
            "desired": d["DesiredCapacity"], "cpu": cpu}


def dump_activities(asg_c, asg_name, path):
    acts = asg_c.describe_scaling_activities(AutoScalingGroupName=asg_name, MaxRecords=100)["Activities"]
    with open(path, "w") as f:
        for a in sorted(acts, key=lambda x: x["StartTime"]):
            f.write(f"{a['StartTime']:%Y-%m-%d %H:%M:%S} [{a['StatusCode']}] {a['Description']}\n    cause: {a.get('Cause','')}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asg", default="bbd-asg")
    ap.add_argument("--region", default="ap-south-1")
    ap.add_argument("--out", default="results/aws/timeline.csv")
    ap.add_argument("--duration", type=int, default=3000)
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--price-per-hour", type=float, default=0.0224)
    a = ap.parse_args()

    asg_c, elb_c, cw_c = (boto3.client(s, region_name=a.region) for s in ("autoscaling", "elbv2", "cloudwatch"))
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    t0, cost, prev = time.time(), 0.0, time.time()
    with open(a.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "t", "launched", "ready", "desired", "avg_util", "avg_inflight", "pressure",
                    "rps_total", "shed_total", "queue_len", "orders_done", "cost_usd"])
        while time.time() - t0 < a.duration:
            s = snapshot(asg_c, elb_c, cw_c, a.asg)
            now = time.time()
            cost += s["launched"] * a.price_per_hour / 3600.0 * (now - prev)
            prev = now
            util = (s["cpu"] or 0.0) / 100.0
            w.writerow([round(now, 3), round(now - t0, 1), s["launched"], s["ready"], s["desired"],
                        round(util, 3), 0, 0, 0, 0, 0, 0, round(cost, 6)])
            fh.flush()
            print(f"t={now - t0:6.0f}s desired={s['desired']:2d} launched={s['launched']:2d} "
                  f"in_service={s['in_service']:2d} healthy={s['ready']:2d} cpu={s['cpu'] and round(s['cpu'], 1)}",
                  flush=True)
            time.sleep(max(0, a.interval - (time.time() - now)))
    dump_activities(asg_c, a.asg, os.path.join(os.path.dirname(a.out) or ".", "scaling_activities.txt"))


if __name__ == "__main__":
    main()

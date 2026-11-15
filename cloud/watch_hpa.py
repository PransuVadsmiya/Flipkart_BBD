"""Poll the Kubernetes Deployment + HPA during a load test and record pod counts and CPU.

Writes a timeline.csv in the same schema as the AWS watcher and the local autoscaler, so
experiments/analyze.py can plot latency against pod count unchanged. At the end it dumps the HPA's
description and the namespace events (evidence that scaling was automatic, like the ASG activity log).
Needs kubectl pointing at the cluster (the kind cluster from k8s/kind/config.yaml) and metrics-server.
"""
import argparse
import csv
import json
import os
import subprocess
import time


def kubectl(*args):
    return subprocess.run(["kubectl", "-n", "bbd", *args], capture_output=True, text=True).stdout


def snapshot():
    try:
        dep = json.loads(kubectl("get", "deploy", "app", "-o", "json"))["status"]
        hpa = json.loads(kubectl("get", "hpa", "app", "-o", "json"))["status"]
    except (json.JSONDecodeError, KeyError):
        return None
    cpu = None
    for m in hpa.get("currentMetrics") or []:
        if m.get("type") == "Resource" and m["resource"]["name"] == "cpu":
            cpu = m["resource"]["current"].get("averageUtilization")
    return {"launched": dep.get("replicas", 0), "ready": dep.get("readyReplicas", 0),
            "desired": hpa.get("desiredReplicas", 0), "cpu": cpu}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/k8s-hpa/timeline.csv")
    ap.add_argument("--duration", type=int, default=1300)
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--price-per-hour", type=float, default=0.0224,
                    help="only fills the cost column (pod-hours x price); use instance_seconds, not dollars")
    a = ap.parse_args()
    out_dir = os.path.dirname(a.out) or "."
    os.makedirs(out_dir, exist_ok=True)
    t0, cost, prev = time.time(), 0.0, time.time()
    with open(a.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "t", "launched", "ready", "desired", "avg_util", "avg_inflight", "pressure",
                    "rps_total", "shed_total", "queue_len", "orders_done", "cost_usd"])
        while time.time() - t0 < a.duration:
            s = snapshot()
            now = time.time()
            if s:
                cost += s["launched"] * a.price_per_hour / 3600.0 * (now - prev)
                util = (s["cpu"] or 0) / 100.0
                w.writerow([round(now, 3), round(now - t0, 1), s["launched"], s["ready"], s["desired"],
                            round(util, 3), 0, 0, 0, 0, 0, 0, round(cost, 6)])
                fh.flush()
                print(f"t={now - t0:6.0f}s desired={s['desired']} pods={s['launched']} ready={s['ready']} "
                      f"cpu={s['cpu']}%", flush=True)
            prev = now
            time.sleep(max(0, a.interval - (time.time() - now)))
    with open(os.path.join(out_dir, "hpa_events.txt"), "w") as f:
        f.write(kubectl("describe", "hpa", "app"))
        f.write("\n--- events ---\n")
        f.write(kubectl("get", "events", "--sort-by=.lastTimestamp"))


if __name__ == "__main__":
    main()

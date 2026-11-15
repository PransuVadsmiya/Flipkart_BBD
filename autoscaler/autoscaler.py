"""Autoscaler control loop.

Modes
  static    fixed N instances for the whole run (baselines: under- and over-provisioned)
  reactive  target tracking on load pressure, fast scale-out / slow scale-in (cloud autoscaling, [3])
  prewarm   reactive + a scheduled capacity floor before the sale starts
            (Flipkart's "prepare months ahead" [1], compressed to seconds here)

Backends
  process   spawns uvicorn processes locally (what the sandbox / quick runs use)
  docker    `docker compose up --scale app=N` (needs the compose stack running)
  k8s       `kubectl scale deploy/app` (needs the k8s/ manifests applied; Service = load balancer)

Every second a row is appended to the timeline CSV (instances, load, queue depth, cost).
Cost = billed instance-seconds * price. Billing starts at launch, so boot time is paid for.
"""
import argparse
import csv
import json
import math
import os
import signal
import subprocess
import sys
import time

import redis

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ProcessBackend:
    def __init__(self, base_port, env):
        self.base_port, self.env, self.procs = base_port, env, {}   # port -> Popen
        self.r = redis.from_url(env["REDIS_URL"], decode_responses=True)

    def count(self):
        return len(self.procs)

    def scale_to(self, n):
        while len(self.procs) < n:
            port = next(p for p in range(self.base_port, self.base_port + 500) if p not in self.procs)
            env = {**os.environ, **self.env, "PORT": str(port), "INSTANCE_ID": f"i{port}",
                   "ADVERTISE_ADDR": f"127.0.0.1:{port}"}
            self.procs[port] = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--no-access-log",
                 "--log-level", "warning"], cwd=ROOT, env=env)
        while len(self.procs) > n:
            port = max(self.procs)                      # newest first
            p = self.procs.pop(port)
            self.r.delete(f"inst:i{port}")              # drain: LB stops routing to it
            time.sleep(1.0)
            p.send_signal(signal.SIGTERM)

    def close(self):
        for p in self.procs.values():
            p.terminate()
        for p in self.procs.values():
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


class DockerBackend:
    def __init__(self, compose_file):
        self.cf, self.n = compose_file, None

    def count(self):
        out = subprocess.run(["docker", "compose", "-f", self.cf, "ps", "-q", "app"],
                             capture_output=True, text=True).stdout.split()
        return len(out)

    def scale_to(self, n):
        subprocess.run(["docker", "compose", "-f", self.cf, "up", "-d", "--no-recreate", "--scale",
                        f"app={n}", "app"], check=True, capture_output=True)

    def close(self):
        pass


class K8sBackend:
    """Scales the `app` Deployment. Kubernetes handles placement, readiness and graceful termination."""

    def __init__(self, namespace, deployment="app"):
        self.ns, self.dep = namespace, deployment

    def count(self):
        out = subprocess.run(["kubectl", "-n", self.ns, "get", "deploy", self.dep, "-o",
                              "jsonpath={.status.replicas}"], capture_output=True, text=True).stdout.strip()
        return int(out) if out.isdigit() else 0     # status.replicas = pods created (billed), ready or not

    def scale_to(self, n):
        subprocess.run(["kubectl", "-n", self.ns, "scale", f"deploy/{self.dep}", f"--replicas={n}"],
                       check=True, capture_output=True)

    def close(self):
        pass


def read_fleet(r):
    insts = []
    for k in r.scan_iter("inst:*"):
        v = r.get(k)
        if v:
            insts.append(json.loads(v))
    return insts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["process", "docker", "k8s"], default="process")
    ap.add_argument("--mode", choices=["static", "reactive", "prewarm"], default="reactive")
    ap.add_argument("--redis", default="redis://localhost:6379/0")
    ap.add_argument("--initial", type=int, default=4, help="instances at t=0 (static: the fixed size)")
    ap.add_argument("--min", type=int, default=3)
    ap.add_argument("--max", type=int, default=20)
    ap.add_argument("--target", type=float, default=0.55, help="target pressure (0-1)")
    ap.add_argument("--interval", type=int, default=5, help="seconds between scaling decisions")
    ap.add_argument("--scale-in-cooldown", type=int, default=15)
    ap.add_argument("--prewarm", default="", help="'start_s:floor:end_s', e.g. 20:10:120")
    ap.add_argument("--price-per-hour", type=float, default=0.0208, help="assumed VM price (USD/h)")
    ap.add_argument("--duration", type=int, default=0, help="stop after N seconds (0 = until SIGTERM)")
    ap.add_argument("--timeline", default="results/timeline.csv")
    ap.add_argument("--base-port", type=int, default=9000)
    ap.add_argument("--k8s-namespace", default="bbd")
    ap.add_argument("--compose-file", default=os.path.join(ROOT, "docker-compose.yml"))
    ap.add_argument("--start-delay", type=float, default=3.0, help="simulated boot time (process backend)")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--service-ms", type=float, default=250)
    a = ap.parse_args()

    r = redis.from_url(a.redis, decode_responses=True)
    if a.backend == "process":
        be = ProcessBackend(a.base_port, {"REDIS_URL": a.redis, "START_DELAY_S": str(a.start_delay),
                                          "CONCURRENCY": str(a.concurrency), "SERVICE_MS": str(a.service_ms)})
    elif a.backend == "k8s":
        be = K8sBackend(a.k8s_namespace)
    else:
        be = DockerBackend(a.compose_file)

    floor_sched = None
    if a.prewarm:
        s, f, e = (int(x) for x in a.prewarm.split(":"))
        floor_sched = (s, f, e)

    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(v=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(v=True))

    os.makedirs(os.path.dirname(a.timeline) or ".", exist_ok=True)
    be.scale_to(a.initial)
    desired, t0, cost, tick = a.initial, time.time(), 0.0, 0
    prev = t0
    last_in = t0   # scale-in cooldown starts at launch: no scale-in before traffic arrives
    window = []   # per-second pressure samples since the last decision (smooths cold-start bursts)
    last_done = int(r.get("orders:done") or 0)
    with open(a.timeline, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "t", "launched", "ready", "desired", "avg_util", "avg_inflight", "pressure",
                    "rps_total", "shed_total", "queue_len", "orders_done", "cost_usd"])
        try:
            while not stop["v"]:
                time.sleep(max(0.0, t0 + tick + 1 - time.time()))
                tick += 1
                now = time.time()
                t = now - t0
                fleet = read_fleet(r)
                ready = len(fleet)
                launched = be.count()
                cost += launched * a.price_per_hour / 3600.0 * (now - prev)   # real elapsed time (docker calls make ticks >1s)
                prev = now
                avg_util = sum(i["util"] for i in fleet) / ready if ready else 0.0
                avg_inflight = sum(i["inflight"] for i in fleet) / ready if ready else 0.0
                # pressure: busy-time fraction, or queueing depth per slot when saturated
                # capped so a transient queue while instances boot cannot request a huge fleet
                pressure = min(1.5, max(avg_util, avg_inflight / (a.concurrency * 4)))
                if ready:
                    window.append(pressure)
                if a.mode != "static" and tick % a.interval == 0 and window:
                    smooth = sum(window) / len(window)
                    window.clear()
                    want = math.ceil(ready * smooth / a.target)
                    floor = a.min
                    if floor_sched and floor_sched[0] <= t < floor_sched[2]:
                        floor = max(floor, floor_sched[1])
                    want = min(want, max(launched * 2, launched + 4))   # step cap: avoid overshoot while instances boot
                    want = max(floor, min(a.max, want))
                    if want > desired:
                        desired = want                              # scale out immediately
                    elif want < desired and now - last_in >= a.scale_in_cooldown:
                        desired = max(want, desired - max(1, desired // 4))   # scale in gently
                        last_in = now
                    if desired != launched:
                        be.scale_to(desired)
                done = int(r.get("orders:done") or 0)
                w.writerow([round(now, 3), round(t, 1), launched, ready, desired, round(avg_util, 3),
                            round(avg_inflight, 2), round(pressure, 3),
                            round(sum(i["rps"] for i in fleet), 1), sum(i["shed"] for i in fleet),
                            r.llen("orders"), done, round(cost, 6)])
                fh.flush()
                if a.duration and t >= a.duration:
                    break
        finally:
            be.close()
    print(f"autoscaler done: cost=${cost:.5f}", flush=True)


if __name__ == "__main__":
    main()

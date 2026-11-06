"""Run one scaling experiment end to end (process mode) and analyse it.

  python experiments/run_experiment.py --name reactive --mode reactive
  python experiments/run_experiment.py --name static-low  --mode static --initial 2
  python experiments/run_experiment.py --name static-peak --mode static --initial 12
  python experiments/run_experiment.py --name prewarm --mode prewarm --prewarm 15:10:110

Starts: redis, order workers, software LB, autoscaler, then the Locust spike test.
Output: results/<name>/{locust_*.csv, timeline.csv, summary.json, plots}
"""
import argparse
import os
import subprocess
import sys
import time

import redis

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def wait_ready(r, n, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if len(list(r.scan_iter("inst:*"))) >= n:
            return True
        time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--mode", default="reactive", choices=["static", "reactive", "prewarm"])
    ap.add_argument("--initial", type=int, default=4)
    ap.add_argument("--max", type=int, default=20)
    ap.add_argument("--prewarm", default="")
    ap.add_argument("--base-rps", type=int, default=40)
    ap.add_argument("--mult", type=float, default=5)
    ap.add_argument("--warm", type=int, default=40)
    ap.add_argument("--ramp", type=int, default=20)
    ap.add_argument("--hold", type=int, default=40)
    ap.add_argument("--down", type=int, default=20)
    ap.add_argument("--tail", type=int, default=60)
    ap.add_argument("--start-delay", type=float, default=3.0)
    ap.add_argument("--redis-port", type=int, default=6390)
    ap.add_argument("--lb-port", type=int, default=8080)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--skip-analyze", action="store_true")
    a = ap.parse_args()

    out = os.path.join(ROOT, "results", a.name)
    os.makedirs(out, exist_ok=True)
    url = f"redis://localhost:{a.redis_port}/0"
    env = {**os.environ, "REDIS_URL": url, "PYTHONPATH": ROOT}
    procs, logs = [], []

    def spawn(cmd, log, **kw):
        f = open(os.path.join(out, log), "w")
        logs.append(f)
        p = subprocess.Popen(cmd, cwd=ROOT, env={**env, **kw.pop("env", {})}, stdout=f, stderr=subprocess.STDOUT)
        procs.append(p)
        return p

    total = a.warm + a.ramp + a.hold + a.down + a.tail
    try:
        spawn(["redis-server", "--port", str(a.redis_port), "--save", "", "--appendonly", "no"], "redis.log")
        r = redis.from_url(url, decode_responses=True)
        for _ in range(50):
            try:
                r.ping()
                break
            except Exception:
                time.sleep(0.2)
        r.flushall()
        for i in range(a.workers):
            spawn([PY, "-m", "app.worker"], f"worker{i}.log", env={"WORKER_ID": f"w{i}"})
        spawn([PY, "-m", "lb.lb", "--port", str(a.lb_port)], "lb.log")

        auto_cmd = [PY, "autoscaler/autoscaler.py", "--mode", a.mode, "--initial", str(a.initial),
                    "--redis", url, "--max", str(a.max), "--timeline", os.path.join(out, "timeline.csv"),
                    "--start-delay", str(a.start_delay), "--duration", str(total + 12)]
        if a.prewarm:
            auto_cmd += ["--prewarm", a.prewarm]
        auto = spawn(auto_cmd, "autoscaler.log")
        print(f"[{a.name}] waiting for {a.initial} instances ...", flush=True)
        if not wait_ready(r, a.initial):
            raise SystemExit("instances did not come up")
        time.sleep(1)

        print(f"[{a.name}] load test running ({total}s) ...", flush=True)
        lenv = {"BASE_RPS": str(a.base_rps), "PEAK_MULT": str(a.mult), "T_WARM": str(a.warm),
                "T_RAMP": str(a.ramp), "T_HOLD": str(a.hold), "T_DOWN": str(a.down), "T_TAIL": str(a.tail)}
        with open(os.path.join(out, "locust.log"), "w") as lf:
            subprocess.run([PY, "-m", "locust", "-f", "loadtest/locustfile.py", "--headless",
                            "--host", f"http://localhost:{a.lb_port}", "--csv", os.path.join(out, "locust"),
                            "--csv-full-history", "--only-summary"],
                           cwd=ROOT, env={**env, **lenv}, stdout=lf, stderr=subprocess.STDOUT)
        # let the order queue drain, then snapshot queue stats
        time.sleep(3)
        done = int(r.get("orders:done") or 0)
        lat = sorted(float(x) for x in r.lrange("orders:lat", 0, -1))
        with open(os.path.join(out, "orders.txt"), "w") as f:
            f.write(f"done={done}\nleft={r.llen('orders')}\n")
            if lat:
                f.write(f"order_p50_s={lat[len(lat)//2]}\norder_p95_s={lat[int(len(lat)*0.95)]}\n")
        auto.wait(timeout=30)
    finally:
        for p in procs[::-1]:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in logs:
            f.close()
        # reap stray app instances if the autoscaler was killed before cleanup
        subprocess.run(["pkill", "-f", "uvicorn app.main:app"], check=False)

    if a.skip_analyze:
        return
    subprocess.run([PY, "experiments/analyze.py", "--run", a.name, "--peak-start", str(a.warm + a.ramp),
                    "--peak-end", str(a.warm + a.ramp + a.hold)], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()

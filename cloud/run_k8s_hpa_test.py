"""One command for the Kubernetes HPA experiment: watcher + Locust + analysis.

  python cloud/run_k8s_hpa_test.py --name k8s-hpa

The same load shape as the AWS runs at half scale (8 -> 40 req/s), because one pod is 1 vCPU, half of a
t3.small. Pods start in seconds, so the profile is shorter than the AWS one; the tail is long enough to
cover the HPA's 300 s scale-in stabilisation window.
Profile (defaults, ~20 min): 2 min normal, 2 min ramp to 5x, 5 min hold, 1 min ramp down, 10 min tail.
"""
import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080", help="the kind NodePort mapping")
    ap.add_argument("--name", default="k8s-hpa")
    ap.add_argument("--base-rps", type=int, default=8)
    ap.add_argument("--mult", type=float, default=5)
    ap.add_argument("--warm", type=int, default=120)
    ap.add_argument("--ramp", type=int, default=120)
    ap.add_argument("--hold", type=int, default=300)
    ap.add_argument("--down", type=int, default=60)
    ap.add_argument("--tail", type=int, default=600)
    ap.add_argument("--price-per-hour", type=float, default=0.0224)
    a = ap.parse_args()
    total = a.warm + a.ramp + a.hold + a.down + a.tail
    out = os.path.join(ROOT, "results", a.name)
    os.makedirs(out, exist_ok=True)

    w = subprocess.Popen([PY, os.path.join(ROOT, "cloud", "watch_hpa.py"), "--out", os.path.join(out, "timeline.csv"),
                          "--duration", str(total + 60), "--price-per-hour", str(a.price_per_hour)], cwd=ROOT)
    time.sleep(10)
    env = {**os.environ, "BASE_RPS": str(a.base_rps), "PEAK_MULT": str(a.mult), "T_WARM": str(a.warm),
           "T_RAMP": str(a.ramp), "T_HOLD": str(a.hold), "T_DOWN": str(a.down), "T_TAIL": str(a.tail)}
    print(f"[{a.name}] load test running for {total/60:.0f} min against {a.url}", flush=True)
    subprocess.run([PY, "-m", "locust", "-f", os.path.join("loadtest", "locustfile.py"), "--headless",
                    "--host", a.url, "--csv", os.path.join(out, "locust"), "--csv-full-history",
                    "--only-summary"], cwd=ROOT, env=env)
    w.wait()
    subprocess.run([PY, os.path.join("experiments", "analyze.py"), "--run", a.name, "--peak-start",
                    str(a.warm + a.ramp), "--peak-end", str(a.warm + a.ramp + a.hold),
                    "--price-per-hour", str(a.price_per_hour)], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()

"""One command for the AWS experiment: watcher + Locust (long cloud profile) + analysis.

  python cloud/run_aws_test.py --url http://<alb-dns> --asg bbd-asg

Profile (defaults, ~38 min): 3 min normal (16 req/s), 3 min ramp to 5x (80 req/s), 10 min hold,
2 min ramp down, 20 min tail. Cloud scaling is slow (1-min metrics, ~2-3 min to alarm, ~2 min to boot,
scale-in waits ~15 min), so the profile is much longer than the local one.
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
    ap.add_argument("--url", required=True, help="http://<alb-dns>")
    ap.add_argument("--asg", default="bbd-asg")
    ap.add_argument("--region", default="ap-south-1")
    ap.add_argument("--name", default="aws")
    ap.add_argument("--base-rps", type=int, default=16)
    ap.add_argument("--mult", type=float, default=5)
    ap.add_argument("--warm", type=int, default=180)
    ap.add_argument("--ramp", type=int, default=180)
    ap.add_argument("--hold", type=int, default=600)
    ap.add_argument("--down", type=int, default=120)
    ap.add_argument("--tail", type=int, default=1200)
    ap.add_argument("--price-per-hour", type=float, default=0.0224, help="USD/h of one t3.small in ap-south-1 (on-demand; verify on the AWS price list)")
    a = ap.parse_args()
    total = a.warm + a.ramp + a.hold + a.down + a.tail
    out = os.path.join(ROOT, "results", a.name)
    os.makedirs(out, exist_ok=True)

    w = subprocess.Popen([PY, os.path.join(ROOT, "cloud", "watch_asg.py"), "--asg", a.asg, "--region", a.region,
                          "--out", os.path.join(out, "timeline.csv"), "--duration", str(total + 90),
                          "--price-per-hour", str(a.price_per_hour)], cwd=ROOT)
    time.sleep(15)
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

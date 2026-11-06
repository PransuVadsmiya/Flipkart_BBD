"""Steady-state sweep: fixed fleet size N under constant load, one run per N.

Gives the clean 'response time vs instance count' curve (the exercise's required plot): unlike the spike
run, load is constant and the fleet does not change, so each point is a steady state.

  python experiments/sweep.py --rps 150 --sizes 4 5 6 8 10 12 16
"""
import argparse
import os
import subprocess
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rps", type=int, default=150)
    ap.add_argument("--sizes", type=int, nargs="+", default=[4, 5, 6, 8, 10, 12, 16])
    ap.add_argument("--duration", type=int, default=70, help="seconds per run")
    ap.add_argument("--skip", type=int, default=30, help="seconds of warm-up to ignore")
    ap.add_argument("--no-run", action="store_true", help="only re-analyse existing runs")
    a = ap.parse_args()

    rows = []
    for n in a.sizes:
        name = f"sweep-{n}"
        if not a.no_run:
            subprocess.run([sys.executable, "experiments/run_experiment.py", "--name", name, "--mode", "static",
                            "--initial", str(n), "--max", str(max(a.sizes)), "--base-rps", str(a.rps), "--mult", "1",
                            "--warm", str(a.duration), "--ramp", "0", "--hold", "0", "--down", "0", "--tail", "0",
                            "--skip-analyze"], cwd=ROOT, check=True)
        h = pd.read_csv(os.path.join(RES, name, "locust_stats_history.csv"))
        h = h[h["Name"] == "Aggregated"].copy()
        h["t"] = h["Timestamp"].astype(float) - h["Timestamp"].astype(float).min()
        s = h[h["t"] >= a.skip]
        st = pd.read_csv(os.path.join(RES, name, "locust_stats.csv"))
        agg = st[st["Name"] == "Aggregated"].iloc[0]
        rows.append({"instances": n, "offered_rps": a.rps,
                     "served_rps": round(float(s["Requests/s"].mean()), 1),
                     "p50_ms": float(pd.to_numeric(s["50%"], errors="coerce").median()),
                     "p95_ms": float(pd.to_numeric(s["95%"], errors="coerce").median()),
                     "p99_ms": float(pd.to_numeric(s["99%"], errors="coerce").median()),
                     "failed_pct": round(100 * float(agg["Failure Count"]) / max(1, float(agg["Request Count"])), 2)})
    df = pd.DataFrame(rows).set_index("instances")
    df.to_csv(os.path.join(RES, "sweep.csv"))
    print(df.to_string())

    fig, ax = plt.subplots(figsize=(7.5, 4.8))
    ax.plot(df.index, df["p50_ms"], "s-", label="p50", color="tab:green")
    ax.plot(df.index, df["p95_ms"], "o-", label="p95", color="tab:red")
    ax.plot(df.index, df["p99_ms"], "^-", label="p99", color="tab:purple", alpha=.7)
    ax.set_yscale("log"); ax.set_xlabel("instances (fixed)"); ax.set_ylabel("response time (ms)")
    ax2 = ax.twinx()
    ax2.bar(df.index, df["failed_pct"], width=0.4, alpha=.25, color="tab:orange", label="failed % (shed)")
    ax2.set_ylabel("failed requests (%)")
    h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper right")
    ax.set_title(f"Steady state at {a.rps} req/s: response time vs instance count")
    fig.tight_layout(); fig.savefig(os.path.join(RES, "sweep_latency_vs_instances.png"), dpi=130)


if __name__ == "__main__":
    main()

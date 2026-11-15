"""Turn one run's raw CSVs into plots + summary.json, or compare several runs.

  python experiments/analyze.py --run reactive --peak-start 40 --peak-end 80
  python experiments/analyze.py --compare static-low static-peak reactive prewarm
"""
import argparse
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results")


def load(run):
    d = os.path.join(RES, run)
    h = pd.read_csv(os.path.join(d, "locust_stats_history.csv"))
    h = h[h["Name"] == "Aggregated"].copy()
    h["ts"] = h["Timestamp"].astype(float)
    tl = pd.read_csv(os.path.join(d, "timeline.csv"))
    h = h.sort_values("ts")
    tol = max(2.0, 1.5 * float(tl["ts"].diff().median() or 1.0))   # cloud timelines are sampled every ~10 s
    m = pd.merge_asof(h, tl.sort_values("ts"), on="ts", direction="nearest", tolerance=tol)
    m["t"] = m["ts"] - m["ts"].min()
    m = m.rename(columns={"50%": "p50", "95%": "p95", "99%": "p99", "Requests/s": "rps", "Failures/s": "fail_rps",
                          "User Count": "users"})
    for c in ["p50", "p95", "p99"]:
        m[c] = pd.to_numeric(m[c], errors="coerce")
    return d, m, tl


def analyse(run, peak_start, peak_end, price=0.0208):
    d, m, tl = load(run)
    st = pd.read_csv(os.path.join(d, "locust_stats.csv"))
    agg = st[st["Name"] == "Aggregated"].iloc[0]
    # only count the window in which the load test ran; warn if the autoscaler timeline does not cover it
    lo, hi = m["ts"].min(), m["ts"].max()
    if tl["ts"].min() > lo + 3 or tl["ts"].max() < hi - 3:
        print(f"WARNING: autoscaler timeline ({tl['ts'].min()-lo:+.0f}s .. {tl['ts'].max()-lo:+.0f}s) does not "
              f"cover the load test (0 .. {hi-lo:.0f}s); cost/instances are partial. Use run_docker.py.")
    tl = tl[(tl["ts"] >= lo - 1) & (tl["ts"] <= hi + 1)].reset_index(drop=True)
    peak = m[(m["t"] >= peak_start) & (m["t"] <= peak_end)]
    n_req, n_fail = int(agg["Request Count"]), int(agg["Failure Count"])
    orders = {}
    op = os.path.join(d, "orders.txt")
    if os.path.exists(op):
        orders = dict(line.strip().split("=") for line in open(op) if "=" in line)
    # time-weighted: rows can be >1 s apart (docker calls are slow), so weight by real elapsed time
    dt = tl["t"].diff().shift(-1).fillna(1.0).clip(lower=0)
    inst_s = float((tl["launched"] * dt).sum())
    span = float(tl["t"].iloc[-1] - tl["t"].iloc[0]) + 1.0
    if "done" not in orders:   # docker mode: no orders.txt, use the autoscaler's last reading
        orders = {"done": int(tl["orders_done"].iloc[-1]), "left": int(tl["queue_len"].iloc[-1])}
    summary = {
        "run": run,
        "requests": n_req,
        "failure_rate_pct": round(100 * n_fail / max(1, n_req), 3),
        "overall_p50_ms": float(agg["50%"]), "overall_p95_ms": float(agg["95%"]), "overall_p99_ms": float(agg["99%"]),
        "peak_p50_ms": round(float(peak["p50"].median()), 1),
        "peak_p95_ms": round(float(peak["p95"].median()), 1),
        "peak_p95_worst_ms": round(float(peak["p95"].max()), 1),
        "peak_rps": round(float(peak["rps"].max()), 1),
        "max_instances": int(tl["launched"].max()),
        "avg_instances": round(inst_s / span, 2),
        "instance_seconds": int(inst_s),
        "cost_usd": round(inst_s * price / 3600.0, 6),
        "orders_processed": int(orders.get("done", 0)), "orders_left_in_queue": int(orders.get("left", 0)),
        "order_p95_s": float(orders["order_p95_s"]) if "order_p95_s" in orders else None,
    }
    json.dump(summary, open(os.path.join(d, "summary.json"), "w"), indent=2)

    fig, ax = plt.subplots(3, 1, figsize=(10, 10), sharex=True)
    ax[0].plot(m["t"], m["users"], label="offered load (req/s)", color="tab:gray")
    ax[0].plot(m["t"], m["rps"], label="served req/s", color="tab:blue")
    ax[0].set_ylabel("requests / s"); ax[0].legend(); ax[0].set_title(f"{run}: load, latency, capacity")
    ax[1].plot(m["t"], m["p50"], label="p50"); ax[1].plot(m["t"], m["p95"], label="p95")
    ax[1].plot(m["t"], m["p99"], label="p99", alpha=.6)
    ax[1].set_ylabel("response time (ms)"); ax[1].set_yscale("log"); ax[1].legend()
    ax[2].step(m["t"], m["launched"], where="post", label="launched (billed)")
    ax[2].step(m["t"], m["ready"], where="post", label="ready (serving)")
    ax[2].set_ylabel("instances"); ax[2].set_xlabel("time (s)"); ax[2].legend()
    for a_ in ax:
        a_.axvspan(peak_start, peak_end, color="orange", alpha=.08)
    fig.tight_layout(); fig.savefig(os.path.join(d, "timeline.png"), dpi=130); plt.close(fig)

    # response time vs instance count (the exercise's required plot)
    g = m.dropna(subset=["ready", "p95"]).copy()
    g["ready"] = g["ready"].astype(int)
    by = g.groupby("ready")[["p50", "p95"]].median()
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.scatter(g["ready"], g["p95"], alpha=.25, s=14, label="p95 (per second)")
    ax.plot(by.index, by["p95"], "o-", color="tab:red", label="p95 median")
    ax.plot(by.index, by["p50"], "s-", color="tab:green", label="p50 median")
    ax.set_xlabel("serving instances"); ax.set_ylabel("response time (ms)"); ax.set_yscale("log")
    ax.set_title(f"{run}: response time vs instance count (whole run)"); ax.legend()
    fig.tight_layout(); fig.savefig(os.path.join(d, "latency_vs_instances.png"), dpi=130); plt.close(fig)

    # Peak-load only: offered load is (nearly) constant here, so latency differences come from the fleet size,
    # not from the load changing. This is the clean version of the required plot.
    pk = g[g["users"] >= 0.9 * g["users"].max()]
    if len(pk):
        by = pk.groupby("ready")[["p50", "p95", "p99"]].median()
        n = pk.groupby("ready").size()
        fig, ax = plt.subplots(figsize=(7.5, 4.8))
        ax.scatter(pk["ready"], pk["p95"], alpha=.25, s=14, color="tab:red", label="p95 (per second)")
        ax.plot(by.index, by["p95"], "o-", color="tab:red", label="p95 median")
        ax.plot(by.index, by["p50"], "s-", color="tab:green", label="p50 median")
        for k in by.index:
            ax.annotate(f"n={n[k]}s", (k, by.loc[k, "p95"]), textcoords="offset points", xytext=(6, 8), fontsize=8)
        ax.set_xlabel("serving (healthy) instances"); ax.set_ylabel("response time (ms)"); ax.set_yscale("log")
        ax.set_xticks(sorted(by.index))
        ax.set_title(f"{run}: response time vs instance count at peak load (~{int(pk['users'].max())} req/s)")
        ax.legend(); fig.tight_layout()
        fig.savefig(os.path.join(d, "latency_vs_instances_peak.png"), dpi=130); plt.close(fig)
        summary["peak_by_instances"] = {int(k): {"seconds": int(n[k]), "p50_ms": float(by.loc[k, "p50"]),
                                                  "p95_ms": float(by.loc[k, "p95"])} for k in by.index}
        json.dump(summary, open(os.path.join(d, "summary.json"), "w"), indent=2)
    print(json.dumps(summary, indent=2))


def compare(runs):
    rows = [json.load(open(os.path.join(RES, r, "summary.json"))) for r in runs]
    df = pd.DataFrame(rows).set_index("run")
    df.to_csv(os.path.join(RES, "comparison.csv"))
    fig, ax = plt.subplots(1, 4, figsize=(16, 4))
    df["peak_p95_ms"].plot.bar(ax=ax[0], color="tab:red"); ax[0].set_title("peak p95 latency (ms)"); ax[0].set_yscale("log")
    df["failure_rate_pct"].plot.bar(ax=ax[1], color="tab:orange"); ax[1].set_title("failed requests (%)")
    df["cost_usd"].plot.bar(ax=ax[2], color="tab:green"); ax[2].set_title("compute cost (USD, assumed price)")
    df["avg_instances"].plot.bar(ax=ax[3], color="tab:blue"); ax[3].set_title("average instances")
    for a_ in ax:
        a_.tick_params(axis="x", rotation=30); a_.set_xlabel("")
    fig.tight_layout(); fig.savefig(os.path.join(RES, "comparison.png"), dpi=130)
    print(df.T.to_string())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run")
    ap.add_argument("--peak-start", type=float, default=40)
    ap.add_argument("--peak-end", type=float, default=80)
    ap.add_argument("--compare", nargs="+")
    ap.add_argument("--price-per-hour", type=float, default=0.0208)
    a = ap.parse_args()
    if a.compare:
        compare(a.compare)
    else:
        analyse(a.run, a.peak_start, a.peak_end, a.price_per_hour)

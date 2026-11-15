# Cloud_Demo: Flipkart Big Billion Days scalability exercise (IT457, Group 30)

Case: Flipkart's move from own data centres / private cloud towards a hybrid setup, and how it
survives the Big Billion Days (BBD) traffic spike (Week 1 report: Group30_1.pdf; mapping table below).
Exercise (faculty theme "Scalability"): web app behind a load balancer with autoscaling, load test,
plot response time against instance count.

## What is built and which Flipkart practice it mirrors

| Flipkart practice (Week 1 source) | In this repo |
|---|---|
| Software LB, no hardware appliances [2] | `lb/nginx.conf` (Docker) and `lb/lb.py` (dev mode), backends discovered dynamically |
| Spike >5x normal traffic [2], load tests at 5x [1] | `loadtest/locustfile.py` BBD-shaped profile (warm, ramp, hold at 5x, ramp down); `loadtest/k6_spike.js` |
| Autoscaling / elastic capacity [3][4] | `autoscaler/autoscaler.py` target-tracking, fast scale-out, slow scale-in |
| Preparation months ahead, pre-provisioning [1] | `prewarm` mode: scheduled capacity floor before the sale |
| Async queues decouple heavy work [1] | `/api/checkout` returns 202 and enqueues; `app/worker.py` drains the queue |
| Caching layer [4] | Redis cache-aside on `/api/products` |
| Fallbacks / graceful degradation [1] | load shedding with 503 + Retry-After ("waiting room"), queue backpressure |
| 2014 stock-outs / correctness [5] | atomic Redis `DECRBY` stock, no overselling under concurrency |
| Shared Kubernetes platform, minutes-not-days deploys [4] | `k8s/` manifests: Deployments, Service (kube-proxy load balancing), readiness probes |
| Cost efficiency of autoscaling [3][4] | instance-seconds x price per run, compared against static provisioning |

Only discussed, not replicated: own data centres, the hybrid migration, Aerospike-scale infrastructure.

## AWS deployment (what the faculty asked for)

The faculty brief for Theme 1 asks for a real cloud deployment: live load-balancer URL, Auto Scaling Group screenshots and a response-time-vs-instances plot,
with a rule "add an instance when CPU > 50%". That is `cloud/` (Terraform for ALB + ASG + CloudWatch dashboard, plus the test runner); see `cloud/README.md`.
The local, Docker and Kubernetes modes below are the experiment framework used to compare scaling strategies.

## Layout

```
app/            FastAPI storefront (main.py) + order worker (worker.py)
lb/             nginx.conf (Docker) and lb.py (dev-mode software LB)
k8s/            Kubernetes manifests (kind) - Deployments, Service/LB, readiness probes
cloud/          AWS deployment: Terraform (ALB, ASG at 50% CPU, dashboard), watcher, test runner
autoscaler/     control loop, process and docker backends, cost accounting
loadtest/       locustfile.py (primary), k6_spike.js (alternative)
experiments/    run_experiment.py, run_all.sh, analyze.py
results/        per-run CSVs, plots, summary.json, comparison.png / comparison.csv
```

## How capacity is modelled

Each app instance has `CONCURRENCY` slots (default 2) and heavy requests cost `SERVICE_MS` (250 ms),
so one instance serves roughly 30 requests/s of the shopper mix (60% browse, 25% search, 10% cart,
5% checkout). Because capacity is bounded per instance rather than by the host CPU, scaling
behaviour is reproducible on any laptop. Instances also have a simulated boot delay (`START_DELAY_S`)
so that reacting late is visibly costly, which is the argument for Flipkart-style pre-provisioning.

## Run it

Requirements: Python 3.11+, `pip install -r requirements.txt`, `redis-server` on PATH.

```bash
# dev mode (no Docker): one scenario, ~2 minutes
python experiments/run_experiment.py --name reactive --mode reactive

# all four scenarios + comparison plot, ~12 minutes
experiments/run_all.sh
```

Scenarios: `static-low` (4 instances, sized for normal traffic), `static-peak` (12 instances, provisioned for
the peak all day), `reactive` (autoscaler only), `prewarm` (scheduled floor + autoscaler).

### Docker mode

```bash
docker compose up -d --build                       # redis, 2 app replicas, 2 workers, nginx on :8080
python experiments/run_docker.py --name docker-reactive --mode reactive   # autoscaler + load test + analysis
```

Docker mode was first run on Windows with Docker Desktop (the stack came up and the autoscaler scaled 2 to 20 containers);
it was not testable in the sandbox where the code was written.

### Kubernetes mode (kind)

Flipkart runs its platform on Kubernetes [4]; this mode puts the same app on a cluster. The Service
`app` is the load balancer (kube-proxy spreads connections over pods that pass the readiness probe, so booting
pods receive no traffic, which removes the 502s seen with nginx in Docker mode). The autoscaler runs the same
target-tracking logic and scales the `app` Deployment with `kubectl scale`.

```
winget install Kubernetes.kind        (or: choco install kind; kubectl comes with Docker Desktop)
docker build -t bbd-app:latest .
kind create cluster --name bbd --config k8s/kind/config.yaml
kind load docker-image bbd-app:latest --name bbd
kubectl apply -f k8s/
kubectl -n bbd get pods               # wait until app/worker/redis pods are Running and READY
curl "localhost:8080/api/products?page=1"
python experiments/run_docker.py --backend k8s --name k8s-reactive --mode reactive
kind delete cluster --name bbd
```

With Docker Desktop's built-in Kubernetes instead of kind, skip the kind steps and pass `--host http://localhost:30080`.
Why not the built-in HorizontalPodAutoscaler? It scales on CPU by default, and this app's capacity is slot-based
(simulated work), so CPU does not track load. Our autoscaler applies the same rule as the HPA,
`desired = ceil(current * metric / target)`, to an application metric (utilisation and queue depth). In production this
is what an HPA on custom metrics (Prometheus adapter) or KEDA does.

## Outputs for the report

* `results/<run>/timeline.png`: offered load, served rate, latency percentiles, instance count over time
* `results/<run>/latency_vs_instances.png`: the required response time vs instance count plot
* `results/comparison.png` and `comparison.csv`: latency, failures, cost and average instances per scenario
* `results/<run>/summary.json`: all headline numbers

## Caveats to state in the report

* The price (`--price-per-hour`, default 0.0208 USD/h) is an assumed small-VM on-demand rate, not a Flipkart figure; Flipkart's real spend is not public (Week 1, gaps).
* Instances are processes (dev mode) or containers, not VMs; boot time is simulated.
* Load generator, LB, Redis and instances share one machine, so absolute numbers are indicative; the comparison between scenarios is the result.

## Results (180 s profile: 40 req/s normal, ramp to 200 req/s = 5x, hold 40 s, ramp down)

| Scenario | Failed % | Peak p95 median / worst (ms) | Avg instances | Cost (USD, assumed price) |
|---|---|---|---|---|
| static-low (4) | 13.2 | 1900 / 3200 | 4.0 | 0.0042 |
| static-peak (12) | 0.12 | 560 / 750 | 12.0 | 0.0127 |
| static-8 (right-sized, ~30 req/s per instance) | 0.14 | 580 / 1300 | 8.0 | 0.0084 |
| reactive | 0.28 | 330 / 1200 | 9.8 | 0.0103 |
| prewarm | 0.17 | 485 / 670 | 11.7 | 0.0124 |

Reading: sizing for normal traffic fails under a 5x spike; sizing for the peak works but is the most expensive;
reactive autoscaling is ~18% cheaper than static-peak here but its worst-case latency (1.2 s) occurs during the
ramp while new instances boot; pre-warming trades some cost for the lowest worst-case latency among the scaling
options. Failures are fast 503 responses from load shedding, so percentiles for `static-low` are not comparable to the others.
The cost gap would be far larger on a real day, because a 2-minute run spends a third of its time at peak.
Locust history percentiles are a 10 s sliding window. A mixed workload with only 2 slots per instance shows
head-of-line blocking (cheap requests queue behind 250 ms searches), which is why p50 is high at base load.
Reactive and prewarm both hit the 20-instance cap during the ramp: the step policy overshoots while instances boot.

## Steady-state sweep: response time vs instance count

`python experiments/sweep.py --rps 150 --sizes 4 5 6 8 10 12 16` runs a fixed fleet under constant load (70 s per size,
first 30 s ignored) and writes `results/sweep.csv` and `results/sweep_latency_vs_instances.png`.
Unlike the spike run, load and fleet size are both constant, so each point is a steady state.

| Instances | Served req/s (offered 150) | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|
| 4 | 112.6 | 1200 | 1800 | 2100 |
| 5 | 137.8 | 750 | 1300 | 1500 |
| 6 | 146.0 | 170 | 730 | 1000 |
| 8 | 149.5 | 260 | 730 | 900 |
| 10 | 149.8 | 250 | 580 | 790 |
| 12 | 149.8 | 220 | 520 | 730 |
| 16 | 150.0 | 47 | 460 | 530 |

Capacity (~30 req/s per instance for this mix) puts the knee at 5-6 instances: below it the fleet cannot serve the
offered load (4 instances serve only 113 of 150 req/s) and latency explodes; above it, extra instances mainly cut
queueing delay and head-of-line blocking, with diminishing returns. The p50 dip at 6 vs 8 is run-to-run noise.

## AWS results (the faculty deliverable run)

Stack: ALB + Auto Scaling Group (1 to 4 `t3.small`, ap-south-1), target tracking at 50% average CPU. Load: Locust, 16 req/s
normal, ramp to 80 req/s (5x), hold 10 min, ramp down, 20 min tail (38 min total). The group started this run at 2 instances.

| Metric | Value |
|---|---|
| Requests | 79,984 (Locust console total; `summary.json` shows 79,980 because the CSV is written slightly before the last responses) |
| Failed | 1 (0.001%) |
| Instances | 2 to 4 (cap = 8 vCPU account limit), back to 1 after the load dropped |
| Average billed instances during the test | 3.31 |
| Compute cost of the test window | USD 0.047 at USD 0.0224/h (about 7,493 instance-seconds); excludes the ALB and the time the stack existed before and after the test |
| Peak window p95 (median of per-second values) / worst second | 590 ms / 3,100 ms |

Response time at constant peak load (about 80 req/s), by number of healthy instances (from `latency_vs_instances_peak.png`):

| Healthy instances | Seconds observed | p50 ms | p95 ms |
|---|---|---|---|
| 1 | 103 | 2000 | 2700 |
| 2 | 196 | 180 | 940 |
| 3 | 164 | 32 | 370 |
| 4 | 170 | 29 | 230 |

Notes: the 1-instance row is the overload moment when one of the two original instances dropped out of the load balancer (serving count fell
from 2 to 1 while 4 were launched); seconds within a run are not independent samples, so treat differences as indicative, not statistically tested.
A first attempt asked for 8 instances and AWS refused the extra four ("vCPU limit of 8"), so max size was set to 4 and the load was reduced.
The cross-check: at 40 req/s on 2 instances (first attempt) CPU read about 49%, i.e. one t3.small saturates near 41 req/s, matching the 43 req/s measured locally.

## Metric definitions

* **peak p95 median / worst**: Locust's 10-second sliding-window p95, sampled every second inside the peak window; median and maximum of those samples.
* **avg instances / cost**: billed instances (launched, including booting) integrated over the time the load test ran, times the assumed hourly price.
* **Failed %**: HTTP 5xx from load shedding (503) and from instances being added/removed (502), over all requests.

## Review notes (14 Nov)

* A fleet sized to the measured capacity (static-8) was added. On this 3-minute profile it is cheaper than reactive autoscaling (0.0084 vs 0.0103) with similar latency, so the cost saving of autoscaling is only against an over-sized fixed fleet. Autoscaling's advantage is that it needs no foreknowledge of the peak and drops to a low floor in quiet periods.
* Each scenario was run once.
* AWS run 1: the planned profile sends about 84,480 requests; Locust sent 79,984. The shortfall (about 4,500) matches the two-minute dip to about 40 req/s while one instance was serving: Locust users wait for responses, so slow responses reduce offered load.
* AWS run 2 uses `suspend_az_rebalance = true` (default now). Run it with `--name aws-run2` so run 1 is not overwritten; `terraform apply -var min_size=3` gives a pre-warmed variant.

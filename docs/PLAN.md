# Project segments (exercise part of the Cloud Computing case study)

| Segment | What | Where | Status |
|---|---|---|---|
| S0 | Repo scaffold, README, Flipkart-to-exercise mapping | `README.md` | done |
| S1 | Mini-Flipkart storefront and order worker | `app/` | done |
| S2 | Software load balancer, Docker Compose stack | `lb/`, `docker-compose.yml` | dev LB tested; Docker path untested |
| S3 | Autoscaler: static / reactive / pre-warm, cost accounting | `autoscaler/` | done |
| S4 | BBD-shaped load test (Locust primary, k6 alternative) | `loadtest/` | Locust tested; k6 untested |
| S5 | Experiment runner, plots, comparison | `experiments/` | done |
| S6 | Kubernetes manifests + K8s autoscaler backend, to mirror Flipkart's K8s platform [4] | `k8s/`, `autoscaler/` | written; plumbing tested with a fake kubectl, not yet on a real cluster |
| S6b | AWS deployment per faculty brief: ALB + ASG (50% CPU) + CloudWatch dashboard + test runner | `cloud/` | written; HCL syntax and AWS API calls checked offline; not applied to a real account yet |
| S7 | Report add-ons: results, figures, discussion, viva notes | report | to do |

## Scenarios and the question each answers

1. `static-low`: what happens if capacity is sized for normal traffic (4 instances)? Expect saturation and shed requests.
2. `static-peak`: size for the peak all the time (12 instances). Works, but pays for idle capacity.
3. `reactive`: autoscaler only. Cheaper, but scale-out lags boot time, so the ramp hurts.
4. `prewarm`: scheduled floor before the spike plus autoscaler. This is the closest analogue of Flipkart's
   months-ahead preparation and load testing at 5x [1].

## Likely viva questions

* Why is a reactive autoscaler not enough for a known event like BBD? (boot time, scale-out lag, cold caches)
* Why is checkout asynchronous? (protects the request path, absorbs bursts, queue depth is a scaling signal)
* Why load shedding / a waiting room instead of letting latency grow? (bounded latency for admitted users, protects the fleet)
* What does the experiment not capture about Flipkart? (own DCs, hybrid migration, network, database scale, real prices)
* How was cost computed and why can't Flipkart's real cost be reproduced? (assumed VM price, billed from launch; real spend undisclosed)

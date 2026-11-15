# Kubernetes HPA experiment (comparison with the AWS Auto Scaling Group)

Same app, same CPU rule (50%), same traffic shape at half scale. Run from the repo root (`C:\Cloud_Demo`).

1. Docker Desktop: Settings -> Resources -> give it at least 6 CPUs and 8 GB, then close other heavy apps.
2. Cluster and image (skip what already exists):
   ```
   docker build -t bbd-app:latest .
   kind create cluster --name bbd --config k8s/kind/config.yaml
   kind load docker-image bbd-app:latest --name bbd
   kubectl apply -f k8s/00-namespace.yaml
   ```
3. metrics-server (the HPA needs it; kind needs the insecure-TLS flag):
   ```
   kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
   kubectl patch deployment metrics-server -n kube-system --type=json --patch-file k8s/hpa/metrics-server-patch.json
   kubectl -n kube-system rollout status deployment/metrics-server
   ```
4. App and HPA:
   ```
   kubectl apply -f k8s/hpa/10-app-cpu.yaml
   kubectl apply -f k8s/hpa/20-hpa.yaml
   kubectl -n bbd get pods
   kubectl -n bbd get hpa
   ```
   Wait until two pods are Ready and `kubectl -n bbd top pods` shows CPU values (not an error). `get hpa` should
   show a CPU percentage, not `<unknown>`.
5. Run (about 20 minutes): `python cloud/run_k8s_hpa_test.py --name k8s-hpa`
6. Results appear in `results/k8s-hpa/` (timeline.csv, Locust CSVs, summary.json, plots, hpa_events.txt).
7. Clean up: `kind delete cluster --name bbd`

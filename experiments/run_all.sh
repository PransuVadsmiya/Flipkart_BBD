#!/usr/bin/env bash
# Runs the four scenarios sequentially, then builds the comparison plot. ~15 minutes total.
set -e
cd "$(dirname "$0")/.."
python3 experiments/run_experiment.py --name static-low  --mode static --initial 4
python3 experiments/run_experiment.py --name static-peak --mode static --initial 12
python3 experiments/run_experiment.py --name reactive    --mode reactive
python3 experiments/run_experiment.py --name prewarm     --mode prewarm --prewarm 30:10:130
python3 experiments/analyze.py --compare static-low static-peak reactive prewarm

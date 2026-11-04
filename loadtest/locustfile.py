"""Big-Billion-Days-shaped load: warm-up at normal traffic, ramp to PEAK_MULT x, hold, ramp down.

Source [1]: Flipkart load-tested at 5x projected traffic, [2]: sale traffic is >5x usual.
Each Locust user issues ~1 request/s, so the number of users == target requests/s.

Env: BASE_RPS (normal traffic), PEAK_MULT (default 5), T_WARM, T_RAMP, T_HOLD, T_DOWN, T_TAIL (seconds)
Run: locust -f loadtest/locustfile.py --headless --host http://localhost:8080 --csv results/run
"""
import os
import random

from locust import FastHttpUser, LoadTestShape, constant_throughput, task

BASE = int(os.getenv("BASE_RPS", "40"))
MULT = float(os.getenv("PEAK_MULT", "5"))
T_WARM, T_RAMP, T_HOLD, T_DOWN, T_TAIL = (int(os.getenv(k, d)) for k, d in
                                          [("T_WARM", 40), ("T_RAMP", 20), ("T_HOLD", 40),
                                           ("T_DOWN", 20), ("T_TAIL", 60)])


class Shopper(FastHttpUser):
    wait_time = constant_throughput(1)

    @task(60)
    def browse(self):
        self.client.get(f"/api/products?page={random.randint(1, 20)}", name="/api/products")

    @task(25)
    def search(self):
        self.client.get(f"/api/search?q=phone{random.randint(1, 500)}", name="/api/search")

    @task(10)
    def add_to_cart(self):
        self.client.post("/api/cart", json={"user": f"u{random.randint(1, 10**6)}", "sku": "sku-1"},
                         name="/api/cart")

    @task(5)
    def checkout(self):
        with self.client.post("/api/checkout", json={"sku": f"sku-{random.randint(0, 4)}", "qty": 1},
                              name="/api/checkout", catch_response=True) as resp:
            if resp.status_code in (202, 409):   # accepted or legitimately sold out
                resp.success()


class BigBillionDay(LoadTestShape):
    def tick(self):
        t = self.get_run_time()
        peak = int(BASE * MULT)
        if t < T_WARM:
            users = BASE
        elif t < T_WARM + T_RAMP:
            users = BASE + (peak - BASE) * (t - T_WARM) / T_RAMP
        elif t < T_WARM + T_RAMP + T_HOLD:
            users = peak
        elif t < T_WARM + T_RAMP + T_HOLD + T_DOWN:
            users = peak - (peak - BASE) * (t - T_WARM - T_RAMP - T_HOLD) / T_DOWN
        elif t < T_WARM + T_RAMP + T_HOLD + T_DOWN + T_TAIL:
            users = BASE
        else:
            return None
        return int(users), (8 if t < 10 else 50)   # gentle start: avoid a cold-cache thundering herd

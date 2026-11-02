"""Order worker: drains the Redis queue (asynchronous order path, source [1])."""
import json
import os
import time
import uuid

import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
ORDER_MS = float(os.getenv("ORDER_MS", "15"))
WID = os.getenv("WORKER_ID") or uuid.uuid4().hex[:6]


def main():
    r = redis.from_url(REDIS_URL, decode_responses=True)
    while True:
        item = r.blpop("orders", timeout=1)
        if not item:
            continue
        order = json.loads(item[1])
        time.sleep(ORDER_MS / 1000.0)  # payment/inventory/fulfilment stub
        done = time.time()
        pipe = r.pipeline()
        pipe.incr("orders:done")
        pipe.lpush("orders:lat", round(done - order["ts"], 4))  # enqueue -> processed
        pipe.ltrim("orders:lat", 0, 49999)
        pipe.execute()


if __name__ == "__main__":
    main()

"""Mini-Flipkart storefront used for the Big Billion Days scalability exercise.

Design choices mirror the Week-1 findings:
  * cache-aside product listing (Redis)           -> [4] caching layer
  * checkout is asynchronous: enqueue + 202        -> [1] async queues decouple heavy work
  * atomic flash-sale stock (no overselling)       -> [5] 2014 stock-outs / correctness
  * load shedding ("waiting room") above a limit   -> [1] fallbacks / graceful degradation
  * every instance heartbeats its own load into
    Redis; the LB and autoscaler use that          -> [2] software LB, no hardware appliance
  * bounded per-instance capacity (CONCURRENCY)    -> makes scaling behaviour reproducible on
                                                      any laptop, independent of CPU count
"""
import asyncio
import hashlib
import json
import os
import random
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CONCURRENCY = int(os.getenv("CONCURRENCY", "2"))        # parallel slots per instance
SERVICE_MS = float(os.getenv("SERVICE_MS", "250"))      # cost of a heavy request (search / cache miss)
HIT_MS = float(os.getenv("HIT_MS", "4"))                # cost of a cache hit
CHECKOUT_MS = float(os.getenv("CHECKOUT_MS", "25"))
CACHE_TTL = int(os.getenv("CACHE_TTL", "30"))
MAX_INFLIGHT = int(os.getenv("MAX_INFLIGHT", "40"))     # shed above this (waiting room)
QUEUE_MAX = int(os.getenv("QUEUE_MAX", "20000"))        # order-queue backpressure
START_DELAY_S = float(os.getenv("START_DELAY_S", "0"))  # simulated VM/container boot time
FLASH_STOCK = int(os.getenv("FLASH_STOCK", "1000000"))
WORK_MODE = os.getenv("WORK_MODE", "sleep")             # "sleep": simulated capacity; "cpu": real CPU burn
N_SKUS = 5
INSTANCE_ID = os.getenv("INSTANCE_ID") or uuid.uuid4().hex[:8]
ADVERTISE = os.getenv("ADVERTISE_ADDR") or f"{socket.gethostbyname(socket.gethostname())}:{os.getenv('PORT', '8000')}"

state = {"inflight": 0, "reqs": 0, "shed": 0, "busy_s": 0.0}
slots = asyncio.Semaphore(CONCURRENCY)
r = None   # redis client, or MemStore when REDIS_URL=memory (stateless single-instance mode, used on AWS)

_BUF = os.urandom(64 * 1024)
_POOL = ThreadPoolExecutor(max_workers=max(2, CONCURRENCY))


def _burn(ms: float):
    """Consume `ms` of real CPU time. Uses thread CPU time (not wall time), so under contention the request
    takes longer instead of doing less work, exactly like an overloaded server. sha256 releases the GIL."""
    end = time.thread_time() + ms / 1000.0
    while time.thread_time() < end:
        hashlib.sha256(_BUF).digest()


async def work(ms: float):
    """Occupy one capacity slot for ~ms (queues if all slots are busy)."""
    async with slots:
        t0 = time.monotonic()
        ms = ms * random.uniform(0.8, 1.2)
        if WORK_MODE == "cpu":
            await asyncio.get_running_loop().run_in_executor(_POOL, _burn, ms)
        else:
            await asyncio.sleep(ms / 1000.0)
        state["busy_s"] += time.monotonic() - t0


class MemStore:
    """Minimal in-process stand-in for the Redis calls this app makes (no shared state between instances)."""

    def __init__(self):
        self.kv, self.exp, self.lists = {}, {}, {}

    async def get(self, k):
        if k in self.exp and self.exp[k] < time.time():
            self.kv.pop(k, None); self.exp.pop(k, None)
        return self.kv.get(k)

    async def set(self, k, v, ex=None):
        self.kv[k] = v
        if ex:
            self.exp[k] = time.time() + ex

    async def setnx(self, k, v):
        self.kv.setdefault(k, v)

    async def decrby(self, k, n):
        self.kv[k] = int(self.kv.get(k, 0)) - n
        return self.kv[k]

    async def incrby(self, k, n):
        self.kv[k] = int(self.kv.get(k, 0)) + n
        return self.kv[k]

    async def llen(self, k):
        return len(self.lists.get(k, []))

    async def rpush(self, k, v):
        lst = self.lists.setdefault(k, [])
        lst.append(v)
        if len(lst) > 1000:          # nobody consumes the queue in memory mode: keep it bounded
            del lst[:500]

    async def delete(self, *k):
        for x in k:
            self.kv.pop(x, None)

    async def aclose(self):
        pass


async def heartbeat():
    last_t, last_busy, last_reqs = time.monotonic(), 0.0, 0
    while True:
        await asyncio.sleep(1.0)
        now = time.monotonic()
        dt = now - last_t
        util = min(1.0, (state["busy_s"] - last_busy) / (dt * CONCURRENCY))
        rps = (state["reqs"] - last_reqs) / dt
        last_t, last_busy, last_reqs = now, state["busy_s"], state["reqs"]
        payload = {"id": INSTANCE_ID, "addr": ADVERTISE, "util": round(util, 3), "rps": round(rps, 1),
                   "inflight": state["inflight"], "shed": state["shed"], "ts": time.time()}
        try:
            await r.set(f"inst:{INSTANCE_ID}", json.dumps(payload), ex=5)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    global r
    r = MemStore() if REDIS_URL == "memory" else aioredis.from_url(REDIS_URL, decode_responses=True)
    for i in range(N_SKUS):
        await r.setnx(f"stock:sku-{i}", FLASH_STOCK)
    if START_DELAY_S:
        await asyncio.sleep(START_DELAY_S)  # not registered yet -> receives no traffic
    task = asyncio.create_task(heartbeat())
    yield
    task.cancel()
    try:
        await r.delete(f"inst:{INSTANCE_ID}")
    finally:
        await r.aclose()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def shed_and_count(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    if state["inflight"] >= MAX_INFLIGHT:
        state["shed"] += 1
        return JSONResponse({"error": "busy, you are in the waiting room"}, status_code=503,
                            headers={"Retry-After": "1"})
    state["inflight"] += 1
    state["reqs"] += 1
    try:
        return await call_next(request)
    finally:
        state["inflight"] -= 1


@app.get("/health")
async def health():
    return {"ok": True, "id": INSTANCE_ID}


@app.get("/")
@app.get("/status")
async def status():
    """Simple response for the 'live URL' deliverable; also shows which instance served it."""
    return {"status": "Big Billion Days deal available", "instance": INSTANCE_ID}


@app.get("/api/products")
async def products(page: int = 1):
    key = f"products:{page}"
    cached = await r.get(key)
    if cached:
        await work(HIT_MS)
        return JSONResponse(json.loads(cached), headers={"X-Cache": "HIT", "X-Instance": INSTANCE_ID})
    await work(SERVICE_MS)  # simulated DB query + render
    data = {"page": page, "items": [{"sku": f"sku-{(page * 7 + i) % N_SKUS}", "price": 499 + i * 10}
                                    for i in range(10)]}
    await r.set(key, json.dumps(data), ex=CACHE_TTL)
    return JSONResponse(data, headers={"X-Cache": "MISS", "X-Instance": INSTANCE_ID})


@app.get("/api/search")
async def search(q: str = ""):
    await work(SERVICE_MS)  # always CPU/DB heavy
    return JSONResponse({"q": q, "hits": random.randint(5, 50)}, headers={"X-Instance": INSTANCE_ID})


@app.post("/api/cart")
async def cart(request: Request):
    body = await request.json()
    await work(HIT_MS * 2)
    await r.set(f"cart:{body.get('user', 'anon')}", json.dumps(body), ex=600)
    return {"ok": True}


@app.post("/api/checkout")
async def checkout(request: Request):
    body = await request.json()
    sku, qty = body.get("sku", "sku-0"), int(body.get("qty", 1))
    await work(CHECKOUT_MS)
    left = await r.decrby(f"stock:{sku}", qty)       # atomic: no overselling under concurrency
    if left < 0:
        await r.incrby(f"stock:{sku}", qty)
        return JSONResponse({"error": "sold out"}, status_code=409)
    if await r.llen("orders") > QUEUE_MAX:           # backpressure instead of unbounded queue
        await r.incrby(f"stock:{sku}", qty)
        return JSONResponse({"error": "order queue full"}, status_code=503, headers={"Retry-After": "2"})
    order_id = uuid.uuid4().hex
    await r.rpush("orders", json.dumps({"id": order_id, "sku": sku, "qty": qty, "ts": time.time()}))
    return JSONResponse({"order_id": order_id, "status": "accepted"}, status_code=202)

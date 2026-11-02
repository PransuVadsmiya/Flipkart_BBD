"""Dev-mode software load balancer (least-in-flight, pooled keep-alive upstreams, retry).

Stands in for nginx when Docker is not available. Discovers backends from the
`inst:*` heartbeat keys in Redis, so scale-out/in needs no config reload - the
same idea as Flipkart's own-cloud software LBs that avoid hardware appliances [2].

Raw asyncio streams (no HTTP framework) keep per-request overhead low so the LB is
not the bottleneck on a small machine. Assumes HTTP/1.1 with Content-Length bodies,
which is what the app returns.
"""
import argparse
import asyncio
import json
import os

import redis.asyncio as aioredis

try:
    import uvloop
except ImportError:  # optional speed-up
    uvloop = None

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
inflight: dict[str, int] = {}       # addr -> in-flight requests
idle: dict[str, list] = {}          # addr -> pooled (reader, writer)
stats = {"requests": 0, "no_upstream": 0, "retries": 0}


async def discover():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    while True:
        try:
            addrs = set()
            async for key in r.scan_iter("inst:*"):
                v = await r.get(key)
                if v:
                    addrs.add(json.loads(v)["addr"])
            for a in addrs:
                inflight.setdefault(a, 0)
            for a in list(inflight):
                if a not in addrs:
                    inflight.pop(a, None)
                    for _, w in idle.pop(a, []):
                        w.close()
        except Exception:
            pass
        await asyncio.sleep(0.5)


async def read_message(reader):
    head = await reader.readuntil(b"\r\n\r\n")
    clen = 0
    for line in head.split(b"\r\n")[1:]:
        if line[:15].lower() == b"content-length:":
            clen = int(line[15:])
    body = await reader.readexactly(clen) if clen else b""
    return head, body


async def get_conn(addr):
    pool = idle.get(addr)
    if pool:
        return pool.pop()
    host, port = addr.rsplit(":", 1)
    return await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=1.0)


async def forward(head, body):
    tried = set()
    for _ in range(3):
        cands = [a for a in inflight if a not in tried]
        if not cands:
            break
        addr = min(cands, key=lambda a: inflight[a])
        tried.add(addr)
        inflight[addr] += 1
        try:
            for attempt in range(2):          # 2nd attempt = fresh socket if pooled one was stale
                reader, writer = await get_conn(addr)
                try:
                    writer.write(head + body)
                    rhead, rbody = await read_message(reader)
                    if b"connection: close" in rhead.lower():
                        writer.close()
                    else:
                        idle.setdefault(addr, []).append((reader, writer))
                    return rhead + rbody
                except (asyncio.IncompleteReadError, ConnectionError, OSError):
                    writer.close()
            stats["retries"] += 1
        except (asyncio.TimeoutError, OSError):
            stats["retries"] += 1
        finally:
            if addr in inflight:
                inflight[addr] -= 1
    return None


async def handle(creader, cwriter):
    try:
        while True:
            head, body = await read_message(creader)
            stats["requests"] += 1
            if head.startswith(b"GET /_lb/stats"):
                payload = json.dumps({**stats, "upstreams": inflight}).encode()
                cwriter.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                              % len(payload) + payload)
            else:
                resp = await forward(head, body)
                if resp is None:
                    stats["no_upstream"] += 1
                    msg = b'{"error":"no healthy upstream"}'
                    resp = b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(msg) + msg
                cwriter.write(resp)
            await cwriter.drain()
    except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
        pass
    finally:
        cwriter.close()


async def main(port):
    asyncio.create_task(discover())
    server = await asyncio.start_server(handle, "0.0.0.0", port, backlog=2048)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    port = ap.parse_args().port
    if uvloop:
        uvloop.install()
    asyncio.run(main(port))

// Alternative load generator (k6): same BBD-shaped spike, open-model arrival rate.
// Run: k6 run -e BASE=40 -e MULT=5 loadtest/k6_spike.js
import http from 'k6/http';
import { check } from 'k6';

const BASE = Number(__ENV.BASE || 40);
const PEAK = BASE * Number(__ENV.MULT || 5);
const HOST = __ENV.HOST || 'http://localhost:8080';

export const options = {
  scenarios: {
    bbd: {
      executor: 'ramping-arrival-rate',
      startRate: BASE, timeUnit: '1s', preAllocatedVUs: 200, maxVUs: 2000,
      stages: [
        { target: BASE, duration: '40s' },
        { target: PEAK, duration: '20s' },
        { target: PEAK, duration: '40s' },
        { target: BASE, duration: '20s' },
        { target: BASE, duration: '60s' },
      ],
    },
  },
  thresholds: { http_req_duration: ['p(95)<1000'], http_req_failed: ['rate<0.05'] },
};

export default function () {
  const x = Math.random();
  let res;
  if (x < 0.60) res = http.get(`${HOST}/api/products?page=${1 + Math.floor(Math.random() * 20)}`);
  else if (x < 0.85) res = http.get(`${HOST}/api/search?q=phone${Math.floor(Math.random() * 500)}`);
  else if (x < 0.95) res = http.post(`${HOST}/api/cart`, JSON.stringify({ user: 'u' + Math.random(), sku: 'sku-1' }),
                                     { headers: { 'Content-Type': 'application/json' } });
  else res = http.post(`${HOST}/api/checkout`, JSON.stringify({ sku: 'sku-' + Math.floor(Math.random() * 5), qty: 1 }),
                       { headers: { 'Content-Type': 'application/json' } });
  check(res, { 'ok': (r) => r.status === 200 || r.status === 202 || r.status === 409 });
}

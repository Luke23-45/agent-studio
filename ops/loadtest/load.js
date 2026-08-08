// P6-9 — L1 load test: p95 < 1.5s end-to-end, error rate < 1%.
//
// Target: staging. Concurrency reflects §15 L1 (single-region entry tier).
// Usage:
//   k6 run -e BASE_URL=https://staging.neryva.dev ops/loadtest/load.js
import http from "k6/http";
import { check, sleep } from "k6";
import { Rate, Trend } from "k6/metrics";

const BASE = __ENV.BASE_URL || "http://localhost:8000";
const TOKEN = __ENV.API_TOKEN || "";

const errorRate = new Rate("llm_errors");
const e2eLatency = new Trend("e2e_latency", true);

export const options = {
  stages: [
    { duration: "1m", target: 25 },   // warm-up
    { duration: "3m", target: 100 },  // L1 peak concurrency
    { duration: "2m", target: 100 },  // sustain
    { duration: "1m", target: 0 },    // cool-down
  ],
  thresholds: {
    http_req_duration: ["p(95)<1500"],  // §15 L1 end-to-end budget
    http_req_failed: ["rate<0.01"],
    llm_errors: ["rate<0.01"],
    e2e_latency: ["p(95)<1500"],
  },
};

const params = {
  headers: {
    "Content-Type": "application/json",
    ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
  },
};

export default function () {
  const body = JSON.stringify({
    message: "Summarize the quarterly revenue table in two sentences.",
    tenant_slug: "loadtest",
    surface_id: "k6",
    end_user_id: "loadtest-user",
    stream: false,
  });
  const started = Date.now();
  const res = http.post(`${BASE}/api/v1/threads/completions`, body, params);
  const latency = Date.now() - started;

  e2eLatency.add(latency);
  errorRate.add(res.status >= 400);
  check(res, {
    "status 200": (r) => r.status === 200,
    "has reply": (r) => r.json("reply") !== undefined,
  });
  sleep(0.5);
}

// P6-9 — Soak test: 60 minutes at steady load; verifies no degradation
// (leaks, queue buildup, memory creep) over time.
//
// Usage:
//   k6 run -e BASE_URL=https://staging.neryva.dev ops/loadtest/soak.js
import http from "k6/http";
import { check, sleep } from "k6";

const BASE = __ENV.BASE_URL || "http://localhost:8000";
const TOKEN = __ENV.API_TOKEN || "";

export const options = {
  stages: [
    { duration: "5m", target: 20 },
    { duration: "50m", target: 20 },
    { duration: "5m", target: 0 },
  ],
  thresholds: {
    http_req_duration: ["p(95)<1500", "p(99)<3000"],
    http_req_failed: ["rate<0.01"],
    // No sustained growth after the warm-up: slope of p95 between halves
    // must stay flat (checked by CI job over the exported summary).
    iteration_duration: ["max<20000"],
  },
};

const params = {
  headers: {
    "Content-Type": "application/json",
    ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
  },
};

export default function () {
  const res = http.post(
    `${BASE}/api/v1/threads/completions`,
    JSON.stringify({
      message: "List the open action items from the meeting notes.",
      tenant_slug: "loadtest",
      surface_id: "k6-soak",
      end_user_id: "soak-user",
      stream: false,
    }),
    params
  );
  check(res, { "status 200": (r) => r.status === 200 });
  sleep(3);
}

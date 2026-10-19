import { timingSafeEqual } from "node:crypto";

/** This repository is a single-user app. New school-data routes are local-only. */
export function isLocalLmsRequest(request: Request) {
  // localhost, 127.0.0.1 and ::1 are all this computer; treat them as equivalent
  // (Next.js reports "localhost" unless started with -H, while the browser may use 127.0.0.1).
  const LOCAL = ["localhost", "127.0.0.1", "[::1]"];
  const url = new URL(request.url);
  if (!LOCAL.includes(url.hostname)) return false;
  const origin = request.headers.get("origin");
  try {
    if (origin) {
      const o = new URL(origin);
      if (!LOCAL.includes(o.hostname) || o.port !== url.port) return false;
    }
  } catch { return false; }
  return request.headers.get("sec-fetch-site") !== "cross-site";
}

export function hasLmsToken(request: Request) {
  const expected = process.env.LMS_SYNC_TOKEN || "";
  const actual = (request.headers.get("authorization") || "").replace(/^Bearer /, "");
  if (expected.length < 32 || actual.length !== expected.length) return false;
  return timingSafeEqual(Buffer.from(actual), Buffer.from(expected));
}

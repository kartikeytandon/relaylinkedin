import { loadState } from "./store.mjs";

function json(status, body) {
  return {
    statusCode: status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Cache-Control": "no-store",
    },
    body: JSON.stringify(body),
  };
}

function authorized(event) {
  const expected = process.env.ADMIN_PASSWORD || "";
  if (!expected) return false;
  const header = event.headers.authorization || event.headers.Authorization || "";
  const bearer = header.startsWith("Bearer ") ? header.slice(7) : "";
  const query = event.queryStringParameters?.key || "";
  return bearer === expected || query === expected;
}

export async function handler(event) {
  if (event.httpMethod === "OPTIONS") {
    return {
      statusCode: 204,
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
      },
      body: "",
    };
  }

  if (event.httpMethod !== "GET") {
    return json(405, { ok: false, error: "method_not_allowed" });
  }

  if (!process.env.ADMIN_PASSWORD) {
    return json(503, {
      ok: false,
      error: "admin_not_configured",
      message: "Set ADMIN_PASSWORD in Netlify env vars",
    });
  }

  if (!authorized(event)) {
    return json(401, { ok: false, error: "unauthorized" });
  }

  try {
    const state = await loadState();
    const days = state.visits?.days || {};
    const last14 = Object.keys(days)
      .sort()
      .slice(-14)
      .map((d) => ({ day: d, count: days[d] }));

    return json(200, {
      ok: true,
      signups: state.signups || [],
      signup_count: (state.signups || []).length,
      visits: {
        total: state.visits?.total || 0,
        last_14_days: last14,
        recent: (state.visits?.recent || []).slice(0, 50),
      },
    });
  } catch (err) {
    console.error("[admin] load failed", err);
    return json(500, {
      ok: false,
      error: "store_unavailable",
      message: String(err?.message || err),
    });
  }
}

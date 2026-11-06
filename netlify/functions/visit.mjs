import { trackVisit, initStore } from "./store.mjs";

function json(status, body) {
  return {
    statusCode: status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
    },
    body: JSON.stringify(body),
  };
}

export async function handler(event) {
  if (event.httpMethod === "OPTIONS") {
    return {
      statusCode: 204,
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
      },
      body: "",
    };
  }

  if (event.httpMethod !== "POST") {
    return json(405, { ok: false, error: "method_not_allowed" });
  }

  let path = "/";
  let referrer = "";
  try {
    const payload = JSON.parse(event.body || "{}");
    path = String(payload.path || "/").slice(0, 120);
    referrer = String(payload.referrer || "").slice(0, 200);
  } catch {
    /* ignore */
  }

  try {
    initStore(event);
    await trackVisit({
      path,
      referrer,
      user_agent: event.headers["user-agent"] || "",
    });
    return json(200, { ok: true });
  } catch (err) {
    console.error("[visit] failed", err);
    return json(200, { ok: false, error: "store_unavailable" });
  }
}

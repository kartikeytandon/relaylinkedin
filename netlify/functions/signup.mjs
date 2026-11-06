import { WELCOME_HTML, WELCOME_TEXT } from "./templates.mjs";

const SITE_URL = process.env.RELAY_SITE_URL || "https://relay.devkartikey.com";
const FROM = process.env.RELAY_SMTP_FROM || "RELAY <sayhello@relay.devkartikey.com>";
const REPLY_TO = process.env.RELAY_SMTP_REPLY_TO || "sayhello@relay.devkartikey.com";
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// Simple in-process dedupe for warm Lambda instances (best-effort only)
const recent = new Map();
const DEDUPE_MS = 1000 * 60 * 60 * 24; // 24h

function render(raw, email) {
  let out = raw.replace(/\{\{#email\}\}(.*?)\{\{\/email\}\}/gs, email ? "$1" : "");
  return out
    .replaceAll("{{email}}", email)
    .replaceAll("{{site_url}}", SITE_URL.replace(/\/$/, "") + "/");
}

async function sendResend({ to, subject, html, text }) {
  const key = process.env.RESEND_API_KEY;
  if (!key) {
    console.log("[email] skipped — set RESEND_API_KEY");
    return { sent: false, reason: "no_api_key" };
  }

  const payload = {
    from: FROM,
    to: [to],
    subject,
    html,
    text,
  };
  if (!FROM.includes("resend.dev")) {
    payload.reply_to = REPLY_TO;
  }

  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const reason = data.message || data.error || JSON.stringify(data) || "resend_error";
    console.error("[email] resend failed", res.status, data);
    return { sent: false, reason };
  }
  console.log("[email] sent", { to, id: data.id, from: FROM });
  return { sent: true, id: data.id };
}

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

function seenRecently(email) {
  const now = Date.now();
  for (const [k, t] of recent) {
    if (now - t > DEDUPE_MS) recent.delete(k);
  }
  if (recent.has(email)) return true;
  recent.set(email, now);
  return false;
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

  let email = "";
  try {
    const payload = JSON.parse(event.body || "{}");
    email = String(payload.email || "").trim().toLowerCase();
  } catch {
    return json(400, { ok: false, error: "bad_request" });
  }

  if (!EMAIL_RE.test(email)) {
    return json(400, { ok: false, error: "invalid_email" });
  }

  if (seenRecently(email)) {
    return json(200, { ok: true, duplicate: true });
  }

  const html = render(WELCOME_HTML, email);
  const text = render(WELCOME_TEXT, email);

  const welcome = await sendResend({
    to: email,
    subject: "You're on the RELAY waitlist — launch perk locked",
    html,
    text,
  });

  // Always try to notify you (even if welcome failed — so you still capture the lead)
  const notifyTo = process.env.RELAY_NOTIFY_TO || REPLY_TO;
  await sendResend({
    to: notifyTo,
    subject: `[RELAY waitlist] ${email}${welcome.sent ? "" : " (welcome mail failed)"}`,
    html: `<p>New waitlist signup: <strong>${email}</strong></p>
           <p>Welcome email: ${welcome.sent ? "sent" : `failed — ${welcome.reason}`}</p>`,
    text: `New waitlist signup: ${email}\nWelcome email: ${welcome.sent ? "sent" : `failed — ${welcome.reason}`}`,
  }).catch((e) => console.error("[email] notify failed", e));

  return json(200, {
    ok: true,
    duplicate: false,
    email_sent: welcome.sent,
    email_error: welcome.sent ? undefined : welcome.reason,
  });
}

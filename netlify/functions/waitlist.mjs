import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { getStore } from "@netlify/blobs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SITE_URL = process.env.RELAY_SITE_URL || "https://relay.devkartikey.com";
const FROM = process.env.RELAY_SMTP_FROM || "RELAY <sayhello@devkartikey.com>";
const REPLY_TO = process.env.RELAY_SMTP_REPLY_TO || "sayhello@devkartikey.com";
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function loadTemplate(name) {
  const candidates = [
    join(process.cwd(), "waitlist", name),
    join(__dirname, "../../waitlist", name),
    join(__dirname, name),
  ];
  for (const path of candidates) {
    try {
      return readFileSync(path, "utf8");
    } catch {
      /* try next */
    }
  }
  throw new Error(`Missing template ${name}`);
}

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

  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from: FROM,
      to: [to],
      reply_to: REPLY_TO,
      subject,
      html,
      text,
    }),
  });

  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    console.error("[email] resend failed", data);
    return { sent: false, reason: data.message || "resend_error" };
  }
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

  const store = getStore("waitlist-signups");
  const existing = await store.get(email, { type: "json" });
  if (existing) {
    return json(200, { ok: true, duplicate: true });
  }

  await store.setJSON(email, {
    email,
    joined_at: new Date().toISOString(),
    user_agent: event.headers["user-agent"] || "",
  });

  const html = render(loadTemplate("email_welcome.html"), email);
  const text = render(loadTemplate("email_welcome.txt"), email);

  await sendResend({
    to: email,
    subject: "You're on the RELAY waitlist — launch perk locked",
    html,
    text,
  });

  // Notify yourself of new signups
  if (process.env.RESEND_API_KEY) {
    await sendResend({
      to: REPLY_TO,
      subject: `[RELAY waitlist] ${email}`,
      html: `<p>New waitlist signup: <strong>${email}</strong></p>`,
      text: `New waitlist signup: ${email}`,
    }).catch(() => {});
  }

  return json(200, { ok: true, duplicate: false });
}

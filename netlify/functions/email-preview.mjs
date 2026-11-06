import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SITE_URL = process.env.RELAY_SITE_URL || "https://relay.devkartikey.com";

function loadTemplate(name) {
  const candidates = [
    join(__dirname, "../email-templates", name),
    join(process.cwd(), "netlify/email-templates", name),
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

export async function handler() {
  const html = render(loadTemplate("email_welcome.html"), "you@email.com");
  return {
    statusCode: 200,
    headers: { "Content-Type": "text/html; charset=utf-8" },
    body: html,
  };
}

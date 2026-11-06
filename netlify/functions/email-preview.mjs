import { WELCOME_HTML } from "./templates.mjs";

const SITE_URL = process.env.RELAY_SITE_URL || "https://relay.devkartikey.com";

function render(raw, email) {
  let out = raw.replace(/\{\{#email\}\}(.*?)\{\{\/email\}\}/gs, email ? "$1" : "");
  return out
    .replaceAll("{{email}}", email)
    .replaceAll("{{site_url}}", SITE_URL.replace(/\/$/, "") + "/");
}

export async function handler() {
  const html = render(WELCOME_HTML, "you@email.com");
  return {
    statusCode: 200,
    headers: { "Content-Type": "text/html; charset=utf-8" },
    body: html,
  };
}

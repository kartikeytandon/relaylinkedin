#!/usr/bin/env python3
"""Serve the RELAY waitlist page, save signups, and send welcome email (optional SMTP)."""
from __future__ import annotations

import csv
import json
import os
import re
import smtplib
import sys
import threading
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
SIGNUPS = ROOT / "signups.csv"
EMAIL_HTML = ROOT / "email_welcome.html"
EMAIL_TXT = ROOT / "email_welcome.txt"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
HOST = os.environ.get("RELAY_HOST", "127.0.0.1")
SITE_URL = os.environ.get("RELAY_SITE_URL", "https://relay.devkartikey.com")
FROM_EMAIL = os.environ.get("RELAY_SMTP_FROM", "RELAY <sayhello@devkartikey.com>")


def ensure_csv():
    if not SIGNUPS.exists():
        with SIGNUPS.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["email", "joined_at", "user_agent"])


def render_template(raw: str, email: str, site_url: str) -> str:
    """Minimal placeholders: {{email}}, {{site_url}}, and {{#email}}...{{/email}}."""
    block = re.search(r"\{\{#email\}\}(.*?)\{\{/email\}\}", raw, flags=re.S)
    if block:
        inner = block.group(1) if email else ""
        raw = raw[: block.start()] + inner + raw[block.end() :]
    return (
        raw.replace("{{email}}", email)
        .replace("{{site_url}}", site_url.rstrip("/") + "/")
    )


def build_welcome_email(to_email: str) -> tuple[str, str]:
    html = render_template(EMAIL_HTML.read_text(encoding="utf-8"), to_email, SITE_URL)
    text = render_template(EMAIL_TXT.read_text(encoding="utf-8"), to_email, SITE_URL)
    return html, text


def smtp_configured() -> bool:
    return bool(os.environ.get("RELAY_SMTP_HOST"))


def send_welcome_email(to_email: str) -> None:
    if not smtp_configured():
        print(f"[email] skipped (set RELAY_SMTP_HOST to send) → {to_email}")
        return

    host = os.environ["RELAY_SMTP_HOST"]
    port = int(os.environ.get("RELAY_SMTP_PORT", "587"))
    user = os.environ.get("RELAY_SMTP_USER", "")
    password = os.environ.get("RELAY_SMTP_PASS", "")
    from_addr = os.environ.get("RELAY_SMTP_FROM", FROM_EMAIL)
    reply_to = os.environ.get("RELAY_SMTP_REPLY_TO", "sayhello@devkartikey.com")
    use_ssl = os.environ.get("RELAY_SMTP_SSL", "").lower() in {"1", "true", "yes"}

    html, text = build_welcome_email(to_email)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "You're on the RELAY waitlist — launch perk locked"
    msg["From"] = from_addr
    msg["To"] = to_email
    msg["Reply-To"] = reply_to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    # Envelope sender must be a bare address
    envelope_from = re.search(r"<([^>]+)>", from_addr)
    envelope_from = envelope_from.group(1) if envelope_from else from_addr

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
                if user:
                    smtp.login(user, password)
                smtp.sendmail(envelope_from, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                if user:
                    smtp.login(user, password)
                smtp.sendmail(envelope_from, [to_email], msg.as_string())
        print(f"[email] sent → {to_email}")
    except Exception as exc:  # noqa: BLE001 — log and keep signup successful
        print(f"[email] failed → {to_email}: {exc}", file=sys.stderr)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/email-preview":
            html, _ = build_welcome_email("you@company.com")
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def do_POST(self):
        if self.path.rstrip("/") != "/api/waitlist":
            self.send_error(404, "Not found")
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()

        email = ""
        try:
            if ctype == "application/json":
                email = (json.loads(raw.decode("utf-8") or "{}").get("email") or "").strip()
            else:
                data = parse_qs(raw.decode("utf-8"))
                email = (data.get("email") or [""])[0].strip()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json(400, {"ok": False, "error": "bad_request"})

        email = email.lower()
        if not EMAIL_RE.match(email):
            return self._json(400, {"ok": False, "error": "invalid_email"})

        ensure_csv()
        existing = {row[0] for row in csv.reader(SIGNUPS.open(encoding="utf-8")) if row}
        if email in existing:
            return self._json(200, {"ok": True, "duplicate": True})

        ua = self.headers.get("User-Agent", "")[:200]
        with SIGNUPS.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([email, datetime.now(timezone.utc).isoformat(), ua])

        print(f"[waitlist] + {email}")
        threading.Thread(target=send_welcome_email, args=(email,), daemon=True).start()
        return self._json(200, {"ok": True, "duplicate": False})

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    ensure_csv()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"RELAY waitlist → http://{HOST}:{PORT}/  (public: {SITE_URL})")
    print(f"Email preview  → http://{HOST}:{PORT}/email-preview")
    print(f"From address   → {FROM_EMAIL}")
    print(f"Signups file   → {SIGNUPS}")
    if smtp_configured():
        print("Welcome email  → SMTP configured (will send on signup)")
    else:
        print("Welcome email  → preview only (set RELAY_SMTP_HOST to send)")
    print("Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

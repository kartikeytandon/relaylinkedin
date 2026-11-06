# RELAY — LinkedIn outreach CLI + waitlist

Local Chrome automation for job outreach (`linkedin_outreach.py`) and a small waitlist landing page (`waitlist/`).

> Automating LinkedIn can get accounts restricted. Use modest daily caps and your own judgment.

---

## 1) Waitlist page (simple hero + live demo)

Minimal landing page (inspired by clean component libraries like 21st / Beautiful UI).
Emails save to `waitlist/signups.csv` (no extra packages).

```bash
cd "~/Desktop/Old Laptop Dumps/practice_projects/linkedinscript"
python3 -u waitlist/server.py
```

Open: [http://127.0.0.1:8765/](http://127.0.0.1:8765/)

Optional custom port:

```bash
python3 -u waitlist/server.py 9000
```

View signups:

```bash
column -s, -t waitlist/signups.csv | less -S
# or just:
cat waitlist/signups.csv
```

---

## 2) Outreach CLI (Selenium)

### Setup (once)

```bash
cd "~/Desktop/Old Laptop Dumps/practice_projects/linkedinscript"
python3 -m venv .venv
source .venv/bin/activate
pip install selenium
```

Edit CONFIG in `linkedin_outreach.py` (`YOUR_NAME`, message text — no `<<placeholders>>` left for `--live`).

### Every session: start Chrome with debugging

Quit all Chrome windows first, then:

```bash
cd "~/Desktop/Old Laptop Dumps/practice_projects/linkedinscript"
google-chrome --remote-debugging-port=9222 --user-data-dir="$PWD/chrome_profile"
```

Log into LinkedIn in that window. Leave it open.

Check debugging is up:

```bash
curl -s http://127.0.0.1:9222/json/version | head
```

### Run outreach

```bash
source .venv/bin/activate

python linkedin_outreach.py status
python linkedin_outreach.py daily --attach --live   # full daily pass
# or pieces:
python linkedin_outreach.py connect --attach --limit 5
python linkedin_outreach.py check --attach
python linkedin_outreach.py message --attach --live
```

---

## Commands cheatsheet

| Command | Meaning |
|--------|---------|
| `daily --attach --live` | check → message → followup → find (if needed) → connect |
| `connect --attach` | send connection requests |
| `check --attach` | mark who accepted |
| `message --attach --live` | message accepted people |
| `followup --attach --live` | one follow-up after ~6 days |
| `status` / `export` | counts / CSV dump |

`--attach` = reuse the Chrome you started with port `9222`.  
`--live` = actually send messages/follow-ups (connects already send when the confirm button is clicked).

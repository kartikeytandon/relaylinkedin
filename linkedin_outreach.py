#!/usr/bin/env python3
"""
LinkedIn outreach pipeline (Selenium): find -> connect -> check -> message -> follow up.

READ THIS FIRST
- Automating LinkedIn violates its User Agreement. Restriction/ban is a real
  risk and it is your account, not mine.
- Everything is a DRY RUN unless you pass --live. A dry run does all the
  clicking up to (but not including) the final Send, so you can verify it.
- LinkedIn changes its HTML constantly. This was NOT tested against the live
  site, so expect some selectors to break. Always dry-run first.

SETUP (recommended: real Chrome + --attach; Google sign-in works)
    # Newer Chrome refuses debugging on the default profile, so use a dedicated dir:
    pkill -f '/opt/google/chrome/chrome' || true
    google-chrome --remote-debugging-port=9222 --user-data-dir="$PWD/chrome_profile"
    # In that window: open LinkedIn, log in once (Google OK). Keep it open.
    # Other terminal:
    python linkedin_outreach.py daily --attach          # dry run
    python linkedin_outreach.py daily --attach --live

    # Alternate: Selenium launches its own Chrome (Google sign-in usually blocked)
    python linkedin_outreach.py login
    python linkedin_outreach.py daily

COMMANDS
    login      open dedicated Chrome so you can log in (saved in ./chrome_profile)
    find       search recent hiring posts, queue the authors
    add URL..  queue profile URLs by hand (e.g. from "Meet the hiring team")
    connect    send connection requests to queued people (daily cap)
    check      detect who accepted (reads your Connections page once)
    message    message people who accepted
    followup   ONE follow-up to people who haven't replied after N days
    daily      check -> message -> followup -> find (if queue low) -> connect
    status     counts + today's usage
    export     dump everything to outreach_export.csv (import into Google Sheets)

Options: --live, --limit N, --attach [HOST:PORT] (default 127.0.0.1:9222)
"""
import argparse
import csv
import os
import random
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from selenium import webdriver
from selenium.common.exceptions import (NoSuchElementException,
                                        TimeoutException, WebDriverException)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ============================== CONFIG ======================================
YOUR_NAME = "Kartikey"

SEARCH_QUERIES = [
    "hiring software engineer intern",
    "hiring SDE fresher",
    "hiring graduate software engineer",
    "off campus hiring software developer",
]

# No emoji in templates: chromedriver can't type non-BMP characters.
# Only send this AFTER you have applied through the portal.
MESSAGE_TEMPLATE = """Hi {first_name},

Thanks for connecting. I'm {your_name}, a final-year CS student.

I applied for SDE Intern through the careers portal. If you're able to, I'd really appreciate any pointer on what the team looks for in freshers, or who to reach out to.

Thanks,
{your_name}"""

FOLLOWUP_TEMPLATE = """Hi {first_name}, just a quick follow-up on my earlier note. No worries if the timing isn't right. Thanks for your time either way.

{your_name}"""

MAX_CONNECTS_PER_DAY = 15
MAX_MESSAGES_PER_DAY = 20
MAX_FOLLOWUPS_PER_DAY = 10
FOLLOWUP_AFTER_DAYS = 6          # one follow-up only, never more
MIN_DELAY, MAX_DELAY = 25, 70    # seconds between profile actions
# ============================================================================

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "outreach.db"
PROFILE_DIR = BASE / "chrome_profile"
CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/"
DEFAULT_ATTACH = "127.0.0.1:9222"

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT UNIQUE NOT NULL,
  name TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  source TEXT,
  added_at TEXT,
  requested_at TEXT,
  accepted_at TEXT,
  messaged_at TEXT,
  followed_up_at TEXT,
  notes TEXT
);
"""
# status: queued, requested, connected, messaged, followed_up, replied, failed

# --- XPaths (the fragile part: fix here when LinkedIn changes things) -------
LINK_XP = "//a[contains(@href,'/in/') and not(ancestor::header) and not(ancestor::nav)]"
# Prefer vanity-scoped Connect — bare //main//a[…to connect] also matches
# "People also viewed" and can invite the wrong person.
MORE_XPATHS = [
    "//main//button[@aria-label='More actions']",
    "//main//button[normalize-space()='More']",
    "//main//*[@role='button'][normalize-space()='More']",
]
MENU_CONNECT_XPATHS = [
    "//*[@role='menu' or @role='listbox' or contains(@class,'artdeco-dropdown__content')]"
    "//*[contains(@aria-label,'to connect') or normalize-space()='Connect']",
]
SEND_INVITE_XPATHS = [
    "//button[@aria-label='Send without a note']",
    "//button[normalize-space()='Send without a note']",
    "//*[@role='button'][normalize-space()='Send without a note']",
    "//a[@aria-label='Send without a note' or normalize-space()='Send without a note']",
    "//button[@aria-label='Send invitation']",
    "//*[self::button or self::a or @role='button'][normalize-space()='Send invitation']",
    "//*[@role='dialog']//*[self::button or self::a or @role='button']"
    "[normalize-space()='Send' or normalize-space()='Send now' or contains(@aria-label,'Send')]",
]
PENDING_XPATHS = [
    "//main//*[self::button or self::a or @role='button'][contains(@aria-label,'Pending')]",
    "//main//*[self::button or self::a or @role='button'][normalize-space()='Pending']",
]
MESSAGE_XPATHS = [
    "//main//a[contains(@href,'messaging/compose') and normalize-space()='Message']",
    "//main//a[contains(@href,'messaging/compose')]",
    "//main//button[normalize-space()='Message']",
    "//main//a[normalize-space()='Message']",
    "//main//*[self::button or self::a][contains(@aria-label,'Message')]",
]
TEXTBOX_XP = "//div[@role='textbox' and @contenteditable='true']"
SEND_MSG_XPATHS = [
    "//button[contains(@class,'msg-form__send-button')]",
    "//button[contains(@aria-label,'Send') and not(contains(@aria-label,'without'))]",
    "//button[normalize-space()='Send']",
]
SENDER_NAME_XP = "//*[contains(@class,'msg-s-message-group__name')]"
# ----------------------------------------------------------------------------

SELECT_ALL = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL
TITLES = {"dr", "mr", "ms", "mrs", "prof"}


class Stop(Exception):
    """Raised when LinkedIn shows a checkpoint/limit page. Abort everything."""


# ----------------------------- small helpers --------------------------------
def now():
    return datetime.now().isoformat(timespec="seconds")


def today():
    return date.today().isoformat()


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def update(con, pid, **fields):
    fields = {k: v for k, v in fields.items() if v is not None}
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE people SET {sets} WHERE id=?", (*fields.values(), pid))
        con.commit()


def done_today(con, col):
    return con.execute(
        f"SELECT COUNT(*) FROM people WHERE date({col}) = ?", (today(),)
    ).fetchone()[0]


def clean_url(u):
    m = re.search(r"linkedin\.com/in/([^/?#]+)", u or "")
    return f"https://www.linkedin.com/in/{m.group(1)}/" if m else None


def first_name(full):
    parts = [re.sub(r"[^\w'-]", "", p) for p in full.split()]
    parts = [p for p in parts if p and p.lower() not in TITLES]
    if not parts:
        return ""
    p = parts[0]
    return p.title() if p.isupper() or p.islower() else p


def tiny():
    time.sleep(random.uniform(0.8, 2.0))


def pause(lo=MIN_DELAY, hi=MAX_DELAY):
    t = random.uniform(lo, hi)
    print(f"    ... waiting {t:.0f}s")
    time.sleep(t)


# ------------------------------ browser layer -------------------------------
def clear_profile_locks():
    """Remove stale Singleton* files so a previous crash doesn't spawn extra windows."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"):
        p = PROFILE_DIR / name
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            print(f"WARNING: could not remove {p}: {e}")


def make_driver(attach=None):
    """If attach is set (e.g. 127.0.0.1:9222), control an already-running Chrome."""
    opts = webdriver.ChromeOptions()
    if attach:
        opts.add_experimental_option("debuggerAddress", attach)
        print(f"Attaching to existing Chrome at {attach}")
        try:
            return webdriver.Chrome(options=opts)
        except WebDriverException as e:
            raise SystemExit(
                f"Could not attach to Chrome at {attach}.\n\n"
                "Do this:\n"
                "  1) Quit Chrome:  pkill -f '/opt/google/chrome/chrome' || true\n"
                "  2) Start with a NON-default profile (required by Chrome):\n"
                '       google-chrome --remote-debugging-port=9222 '
                '--user-data-dir="$PWD/chrome_profile"\n'
                "  3) Log into LinkedIn in that window, then re-run with --attach\n"
                f"\nOriginal error: {e}"
            ) from None

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("WARNING: running as root makes Chrome unstable (black windows / freezes).")
        print("         Prefer: exit root, then  sudo chown -R $USER:$USER chrome_profile outreach.db")
    clear_profile_locks()
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_argument("--profile-directory=Default")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-session-crashed-bubble")
    opts.add_argument("--disable-restore-session-state")
    opts.add_argument("--remote-debugging-port=0")  # avoid port clashes between runs
    # Wayland + Selenium often paints a black window and hangs the compositor.
    opts.add_argument("--ozone-platform=x11")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    # Chrome refuses to start as root without --no-sandbox.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        opts.add_argument("--no-sandbox")
    return webdriver.Chrome(options=opts)  # Selenium 4.6+ fetches the driver itself


def guard(driver):
    """Abort immediately if LinkedIn is challenging us or we hit a limit."""
    url = driver.current_url.lower()
    try:
        text = driver.find_element(By.TAG_NAME, "body").text[:3000].lower()
    except WebDriverException:
        text = ""
    bad_url = any(k in url for k in ("checkpoint", "challenge", "authwall", "/login", "/uas/"))
    bad_text = any(k in text for k in (
        "unusual activity", "temporarily restricted", "security verification",
        "weekly invitation limit", "verify you're a human"))
    if bad_url or bad_text:
        raise Stop(f"LinkedIn is showing a checkpoint/limit/login page ({driver.current_url}). "
                   "Do NOT retry today. Wait a few days before running again.")


def go(driver, url):
    driver.get(url)
    try:
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "main")))
    except TimeoutException:
        pass
    tiny()
    guard(driver)


def first_visible(driver, xpaths, timeout=4):
    end = time.time() + timeout
    while time.time() < end:
        for xp in xpaths:
            for el in driver.find_elements(By.XPATH, xp):
                try:
                    if el.is_displayed() and el.is_enabled():
                        return el
                except WebDriverException:
                    pass
        time.sleep(0.3)
    return None


def click(driver, el):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    tiny()
    el.click()


def esc(driver):
    ActionChains(driver).send_keys(Keys.ESCAPE).perform()


def get_name(driver):
    # New LinkedIn often has no <h1>; name is the first segment of the top card.
    for xp in ("//main//h1", "//h1", "//*[@data-anonymize='person-name']"):
        try:
            t = driver.find_element(By.XPATH, xp).text.strip()
            if t:
                return t.split("\n")[0].strip()
        except NoSuchElementException:
            pass
    try:
        top = driver.find_element(By.TAG_NAME, "main").text.strip()
        if top:
            # e.g. "vaibhav sable | He/Him | · 1st | AI Engineer | ..."
            candidate = top.split("|", 1)[0].split("\n", 1)[0].strip()
            if candidate:
                return candidate
    except NoSuchElementException:
        pass
    # Document title: "(1) vaibhav sable | LinkedIn"
    title = (driver.title or "").strip()
    title = re.sub(r"^\(\d+\)\s*", "", title)
    title = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title, flags=re.I).strip()
    return title.split("\n", 1)[0].strip()


def top_card_text(driver):
    try:
        return driver.find_element(By.TAG_NAME, "main").text[:900]
    except NoSuchElementException:
        return ""


def collect_links(driver, scrolls=4):
    links = {}
    for _ in range(scrolls):
        els = driver.find_elements(By.XPATH, LINK_XP)
        for el in els:
            try:
                u = clean_url(el.get_attribute("href"))
                if u and (u not in links or not links[u]):
                    links[u] = el.text.strip().split("\n")[0]
            except WebDriverException:
                continue
        if els:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'end'});", els[-1])
            except WebDriverException:
                pass
        time.sleep(random.uniform(1.5, 3.0))
    return links


# ------------------------------- messaging ----------------------------------
def compose_url_from_href(href):
    """Open full Messaging compose (overlay often has no usable textbox)."""
    if not href:
        return None
    parts = urlsplit(href)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    q.pop("interop", None)  # drop msgOverlay
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))


def open_thread(driver):
    btn = first_visible(driver, MESSAGE_XPATHS, 5)
    if not btn:
        return None
    href = btn.get_attribute("href") or ""
    compose = compose_url_from_href(href) if "messaging/compose" in href else None
    if compose:
        go(driver, compose)
    else:
        try:
            click(driver, btn)
        except WebDriverException:
            driver.execute_script("arguments[0].click();", btn)
        tiny()
    return first_visible(driver, [
        TEXTBOX_XP,
        "//div[@role='textbox' and @contenteditable='true' and contains(@aria-label,'message')]",
        "//div[contains(@class,'msg-form__contenteditable')]",
    ], 10)


def type_into(driver, box, text):
    box.click()
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line:
            box.send_keys(line)
        if i < len(lines) - 1:  # plain Enter would send, Shift+Enter is a newline
            ActionChains(driver).key_down(Keys.SHIFT).send_keys(Keys.ENTER).key_up(Keys.SHIFT).perform()


def send_in_thread(driver, box, text, live):
    """Type the message; send only if live. Returns True if it looks sent/OK."""
    type_into(driver, box, text)
    tiny()
    if not live:
        time.sleep(2)
        box.send_keys(SELECT_ALL, "a")
        box.send_keys(Keys.DELETE)
        return True
    send = first_visible(driver, SEND_MSG_XPATHS, 4)
    if not send:
        return False
    click(driver, send)
    time.sleep(2.5)
    guard(driver)
    box2 = first_visible(driver, [TEXTBOX_XP], 3)
    still_there = bool(box2 and text[:15] in box2.text)
    return not still_there


def thread_senders(driver, wait=6):
    end = time.time() + wait
    while time.time() < end:
        names = {e.text.strip() for e in driver.find_elements(By.XPATH, SENDER_NAME_XP) if e.text.strip()}
        if names:
            return names
        time.sleep(0.5)
    return set()


# --------------------------------- steps ------------------------------------
def run_find(driver, con, live, limit):
    per_query = limit or 15
    added = 0
    for q in SEARCH_QUERIES:
        url = ("https://www.linkedin.com/search/results/content/?keywords=" + quote(q) +
               "&datePosted=%22past-24h%22&sortBy=%22date_posted%22")
        print(f"[find] {q}")
        go(driver, url)
        links = collect_links(driver, 4)
        for u, name in list(links.items())[:per_query]:
            cur = con.execute("INSERT OR IGNORE INTO people (url, name, source, added_at) VALUES (?,?,?,?)",
                              (u, name, f"search: {q}", now()))
            added += cur.rowcount
        con.commit()
        pause(8, 20)
    print(f"[find] queued {added} new profiles. Review them with: python {Path(__file__).name} export")


def run_connect(driver, con, live, limit):
    left = MAX_CONNECTS_PER_DAY - done_today(con, "requested_at")
    n = min(left, limit) if limit else left
    if n <= 0:
        print("[connect] daily cap reached.")
        return
    rows = con.execute("SELECT * FROM people WHERE status='queued' ORDER BY id LIMIT ?", (n,)).fetchall()
    for i, row in enumerate(rows):
        print(f"[connect] {row['url']}")
        status, name, note = connect_one(driver, row, live)
        print(f"    -> {status or 'dry-run'} {('(' + note + ')') if note else ''}")
        if status:
            update(con, row["id"], status=status, name=name or row["name"], notes=note,
                   requested_at=now() if status == "requested" else None,
                   accepted_at=now() if status == "connected" else None)
        if i < len(rows) - 1:
            pause()


def invite_sent_toast(driver):
    for el in driver.find_elements(By.XPATH, "//*[@role='alert' or @role='status' or contains(@class,'toast')]"):
        try:
            if el.is_displayed() and "invitation sent" in (el.text or "").lower():
                return True
        except WebDriverException:
            pass
    return False


def profile_vanity(url):
    return (clean_url(url) or url).rstrip("/").split("/")[-1]


def custom_invite_url(vanity):
    return f"https://www.linkedin.com/preload/custom-invite/?vanityName={vanity}"


def profile_has_connect(driver, vanity):
    """True if this profile exposes Connect (top card or More menu)."""
    if first_visible(driver, [
        f"//main//a[contains(@href,'custom-invite') and contains(@href,'vanityName={vanity}')]",
    ], 2):
        return True
    more = first_visible(driver, MORE_XPATHS, 2)
    if not more:
        return False
    click(driver, more)
    tiny()
    found = first_visible(driver, [
        f"//*[@role='menu']//a[contains(@href,'custom-invite') and contains(@href,'vanityName={vanity}')]",
        f"//*[@role='menu']//*[@role='menuitem'][contains(@aria-label,'to connect')]",
        f"//*[@role='menu']//*[@role='menuitem'][normalize-space()='Connect']",
    ], 2)
    esc(driver)
    tiny()
    return bool(found)


def connect_one(driver, row, live):
    go(driver, row["url"])
    name, top = get_name(driver), top_card_text(driver)
    vanity = profile_vanity(row["url"])
    if re.search(r"\b1st\b", top):
        return "connected", name, "already connected"
    if first_visible(driver, PENDING_XPATHS, 2) or re.search(r"\bPending\b", top):
        return "requested", name, "already pending"
    if not profile_has_connect(driver, vanity):
        return "failed", name, "no Connect option (Follow-only profile?)"

    # New LinkedIn: the confirm UI is a dedicated page. Clicking the More-menu
    # <a> often no-ops; navigating to the custom-invite URL is reliable.
    go(driver, custom_invite_url(vanity))
    time.sleep(1.5)

    if driver.find_elements(By.XPATH, "//input[@type='email']"):
        esc(driver)
        return "failed", name, "asks for their email to connect"

    # Instant-send path (rare on this page, common on in-feed Connect).
    if first_visible(driver, PENDING_XPATHS, 2) or invite_sent_toast(driver):
        note = "sent immediately (no confirm dialog)"
        if not live:
            note = "WARNING: invite already sent — UI has no dry-run confirm step"
        return "requested", name, note

    send = first_visible(driver, SEND_INVITE_XPATHS, 6)
    if not send:
        esc(driver)
        return "failed", name, "invite dialog not recognised (update SEND_INVITE_XPATHS)"
    # Always click — LinkedIn's invite confirm IS this button (no useful dry-run step).
    click(driver, send)
    tiny()
    time.sleep(1.5)
    guard(driver)
    return "requested", name, "clicked Send without a note"


def run_check(driver, con, live, limit):
    rows = con.execute("SELECT * FROM people WHERE status='requested'").fetchall()
    if not rows:
        print("[check] nothing pending.")
        return
    pending = {r["url"].lower(): r for r in rows}
    print(f"[check] reading Connections page ({len(pending)} pending)")
    go(driver, CONNECTIONS_URL)
    links = collect_links(driver, 6)
    hits = 0
    for u in links:
        row = pending.get(u.lower())
        if row:
            update(con, row["id"], status="connected", accepted_at=now())
            hits += 1
            print(f"    accepted: {row['name'] or u}")
    print(f"[check] {hits} new acceptances.")


def run_message(driver, con, live, limit):
    left = MAX_MESSAGES_PER_DAY - done_today(con, "messaged_at")
    n = min(left, limit) if limit else left
    if n <= 0:
        print("[message] daily cap reached.")
        return
    rows = con.execute("SELECT * FROM people WHERE status='connected' AND messaged_at IS NULL "
                       "ORDER BY accepted_at LIMIT ?", (n,)).fetchall()
    for i, row in enumerate(rows):
        print(f"[message] {row['url']}")
        go(driver, row["url"])
        name = get_name(driver) or (row["name"] or "")
        fn = first_name(name)
        if not fn:
            print("    -> skipped, couldn't read their name")
            continue
        box = open_thread(driver)
        if not box:
            print("    -> failed (no message box found)")
            update(con, row["id"], status="failed", notes="no message box found")
            continue
        text = MESSAGE_TEMPLATE.format(first_name=fn, your_name=YOUR_NAME)
        ok = send_in_thread(driver, box, text, live)
        print(f"    -> {'sent' if live and ok else 'dry-run OK' if ok else 'FAILED'}")
        if live:
            update(con, row["id"], name=name, status="messaged" if ok else "failed",
                   messaged_at=now(), notes=None if ok else "send failed, check manually")
        if i < len(rows) - 1:
            pause()


def run_followup(driver, con, live, limit):
    left = MAX_FOLLOWUPS_PER_DAY - done_today(con, "followed_up_at")
    n = min(left, limit) if limit else left
    if n <= 0:
        print("[followup] daily cap reached.")
        return
    cutoff = (datetime.now() - timedelta(days=FOLLOWUP_AFTER_DAYS)).isoformat()
    rows = con.execute("SELECT * FROM people WHERE status='messaged' AND followed_up_at IS NULL "
                       "AND messaged_at <= ? ORDER BY messaged_at LIMIT ?", (cutoff, n)).fetchall()
    for i, row in enumerate(rows):
        print(f"[followup] {row['url']}")
        go(driver, row["url"])
        fn = first_name(get_name(driver) or (row["name"] or ""))
        box = open_thread(driver) if fn else None
        if not box:
            print("    -> skipped, couldn't open thread")
            continue
        senders = thread_senders(driver)
        print(f"    senders seen in thread: {senders or 'none'}")
        if not senders:
            print("    -> skipped, couldn't read the thread (fail-safe: NOT sending)")
            continue
        if len(senders) >= 2:  # heuristic: a second sender means they replied
            print("    -> they replied, no follow-up")
            if live:
                update(con, row["id"], status="replied")
            continue
        text = FOLLOWUP_TEMPLATE.format(first_name=fn, your_name=YOUR_NAME)
        ok = send_in_thread(driver, box, text, live)
        print(f"    -> {'sent' if live and ok else 'dry-run OK' if ok else 'FAILED'}")
        if live:
            update(con, row["id"], status="followed_up" if ok else "failed", followed_up_at=now())
        if i < len(rows) - 1:
            pause()


def run_daily(driver, con, live, limit):
    run_check(driver, con, live, None)
    pause(8, 20)
    run_message(driver, con, live, None)
    run_followup(driver, con, live, None)
    queued = con.execute("SELECT COUNT(*) FROM people WHERE status='queued'").fetchone()[0]
    if queued < MAX_CONNECTS_PER_DAY:
        run_find(driver, con, live, None)
    run_connect(driver, con, live, None)


def run_login(driver, con, live, limit):
    # Google blocks sign-in inside Selenium Chrome ("browser may not be secure").
    # Prefer email/password here, or use --attach with your normal Chrome instead.
    driver.get("https://www.linkedin.com/login")
    print("Use email/password on this page (Google sign-in is blocked in automated Chrome).")
    print("Or Ctrl+C and use:  google-chrome --remote-debugging-port=9222")
    print("then:               python linkedin_outreach.py daily --attach")
    input("When LinkedIn feed is visible, press Enter here... ")
    print("Session saved in ./chrome_profile")


# ------------------------------ no-browser cmds ------------------------------
def add_urls(con, urls):
    n = 0
    for u in urls:
        c = clean_url(u)
        if c:
            n += con.execute("INSERT OR IGNORE INTO people (url, source, added_at) VALUES (?,?,?)",
                             (c, "manual", now())).rowcount
    con.commit()
    print(f"Added {n} profile(s).")


def show_status(con):
    for r in con.execute("SELECT status, COUNT(*) c FROM people GROUP BY status ORDER BY c DESC"):
        print(f"  {r['status']:<12} {r['c']}")
    print(f"Today: {done_today(con, 'requested_at')}/{MAX_CONNECTS_PER_DAY} requests, "
          f"{done_today(con, 'messaged_at')}/{MAX_MESSAGES_PER_DAY} messages, "
          f"{done_today(con, 'followed_up_at')}/{MAX_FOLLOWUPS_PER_DAY} follow-ups")


def export_csv(con):
    out = BASE / "outreach_export.csv"
    rows = con.execute("SELECT * FROM people ORDER BY id").fetchall()
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(rows[0].keys() if rows else ["id", "url", "name", "status"])
        w.writerows([tuple(r) for r in rows])
    print(f"Wrote {out} ({len(rows)} rows)")


def check_templates(live):
    blob = YOUR_NAME + MESSAGE_TEMPLATE + FOLLOWUP_TEMPLATE
    if "<<" in blob:
        if live:
            sys.exit("Fill in every <<placeholder>> in CONFIG before using --live.")
        print("WARNING: <<placeholders>> still present in CONFIG (fine for a dry run, fatal for --live).")


# ----------------------------------- main -----------------------------------
def main():
    p = argparse.ArgumentParser(description="LinkedIn outreach pipeline (dry run unless --live)")
    p.add_argument("command", choices=["login", "find", "add", "connect", "check", "message",
                                       "followup", "daily", "status", "export"])
    p.add_argument("urls", nargs="*")
    p.add_argument("--live", action="store_true", help="actually send; default is dry run")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--attach", nargs="?", const=DEFAULT_ATTACH, default=None,
                   metavar="HOST:PORT",
                   help=f"reuse already-running Chrome (default {DEFAULT_ATTACH})")
    args = p.parse_args()

    con = db()
    if args.command == "status":
        return show_status(con)
    if args.command == "export":
        return export_csv(con)
    if args.command == "add":
        return add_urls(con, args.urls)

    if args.command != "login":
        check_templates(args.live)
        print("MODE:", "LIVE" if args.live else "DRY RUN (nothing is sent; add --live to send)")

    runners = {"login": run_login, "find": run_find, "connect": run_connect, "check": run_check,
               "message": run_message, "followup": run_followup, "daily": run_daily}
    driver = make_driver(attach=args.attach)
    try:
        runners[args.command](driver, con, args.live, args.limit)
    except Stop as e:
        print(f"\nSTOPPED: {e}")
        sys.exit(2)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        # Never quit() an attached Chrome — that would close the user's real browser.
        if args.attach:
            print("Leaving your Chrome open (attached mode).")
        else:
            driver.quit()


if __name__ == "__main__":
    main()
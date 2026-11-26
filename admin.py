# admin.py
#
# Password-protected admin page for sending the Misbah SMS reminders from any
# browser (phone or laptop), instead of from scripts on one laptop.
#
#   /admin              write a message, send a sample, see past sends, add numbers
#   /sms/status         Twilio delivery-status callbacks (no password; updates by message SID)
#
# Flow: write text -> sample to testers -> pick "now" or a date/time (US Eastern)
#       -> preview with recipient count -> confirm -> scheduled/sent by Twilio.
#
# Environment variables (set in Railway):
#   ADMIN_PASSWORD                 required; the admin page is off without it
#   ADMIN_USER                     optional, default "admin" (the sign-in username)
#
# Sign-in uses a login page and a signed cookie that lasts 30 days; "Log out"
# clears it. Changing ADMIN_PASSWORD signs every browser out.
#   TWILIO_MESSAGING_SERVICE_SID   required for sending/scheduling (starts with MG)
#   TESTERS_ONLY                   optional; "1" sends every run to testers only (for trials)

import hashlib
import hmac
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import phonenumbers
import psycopg2
from flask import Response, redirect, request, session
from phonenumbers import PhoneNumberFormat
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
MESSAGING_SERVICE_SID = os.environ.get("TWILIO_MESSAGING_SERVICE_SID", "")
TESTERS_ONLY = os.environ.get("TESTERS_ONLY", "") == "1"

client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

# Twilio accepts scheduled sends 15 minutes to 35 days ahead. Scheduling
# everyone takes a minute or two, so ask for a little more than 15 minutes.
MIN_LEAD = timedelta(minutes=20)
MAX_LEAD = timedelta(days=35)
PRICE_PER_PART = 0.0083  # USD, from past toll-free sends

FINAL_STATUSES = ("delivered", "undelivered", "failed", "canceled")
WELCOME_TEXT = ("You've been added to Misbah event reminders. "
                "Reply STOP to opt out, HELP for help.")
ERROR_MEANINGS = {
    21610: "replied STOP earlier",
    30003: "phone unreachable or off",
    30005: "number does not exist",
    30006: "landline or carrier can't receive SMS",
    30007: "blocked by carrier as spam",
    30008: "unknown carrier error",
}


# ----------------------------
# DATABASE
# ----------------------------
def get_conn():
    return psycopg2.connect(DATABASE_URL)


def query(sql, params=(), one=False):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return None
            return cur.fetchone() if one else cur.fetchall()


def init_admin_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS is_tester BOOLEAN NOT NULL DEFAULT FALSE
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sends (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    body TEXT NOT NULL,
                    status TEXT NOT NULL,
                    send_at TIMESTAMPTZ,
                    recipients INTEGER NOT NULL DEFAULT 0,
                    note TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS send_messages (
                    send_id INTEGER NOT NULL REFERENCES sends(id),
                    phone TEXT NOT NULL,
                    sid TEXT,
                    status TEXT NOT NULL,
                    error_code INTEGER,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (send_id, phone)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS send_messages_sid_idx
                ON send_messages (sid)
            """)
        conn.commit()


# ----------------------------
# HELPERS
# ----------------------------
GSM_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM_EXTENDED = set("^{}\\[~]|€\f")


def sms_parts(text):
    """How many SMS parts Twilio will bill for this text."""
    if all(c in GSM_BASIC or c in GSM_EXTENDED for c in text):
        length = len(text) + sum(1 for c in text if c in GSM_EXTENDED)
        return 1 if length <= 160 else math.ceil(length / 153)
    units = len(text.encode("utf-16-le")) // 2  # emoji count as 2
    return 1 if units <= 70 else math.ceil(units / 67)


def to_e164(raw):
    try:
        parsed = phonenumbers.parse(str(raw), "US")
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)


def fmt_time(dt):
    if dt is None:
        return "right away"
    et, ct = dt.astimezone(ET), dt.astimezone(CT)
    return (f"{et.strftime('%a %b %d, %Y, %I:%M %p')} ET "
            f"({ct.strftime('%I:%M %p')} CT)")


def recipients_list():
    """Testers first so you see the first message arrive."""
    where = "is_tester" if TESTERS_ONLY else "opted_in"
    rows = query(f"SELECT phone_e164 FROM subscribers WHERE {where} "
                 "ORDER BY is_tester DESC, phone_e164")
    return [r[0] for r in rows]


def testers_list():
    return [r[0] for r in query(
        "SELECT phone_e164 FROM subscribers WHERE is_tester ORDER BY phone_e164")]


def status_callback_url():
    host = request.host.split(":")[0]
    if host in ("localhost", "127.0.0.1"):
        return None  # Twilio can't reach a laptop
    return f"https://{request.host}/sms/status"


def parse_when(form):
    """Returns (send_at or None for now, error message or None)."""
    if form.get("when") == "now":
        return None, None
    raw = (form.get("at") or "").strip()
    try:
        send_at = datetime.fromisoformat(raw).replace(tzinfo=ET)
    except ValueError:
        return None, "Please pick a date and time."
    lead = send_at - datetime.now(timezone.utc)
    if lead < MIN_LEAD:
        return None, "The time must be at least 20 minutes from now (Twilio's rule). Pick a later time or choose Now."
    if lead > MAX_LEAD:
        return None, "The time must be within 35 days."
    return send_at, None


# ----------------------------
# BACKGROUND JOBS
# ----------------------------
def run_send(send_id, callback_url):
    """Create one Twilio message per recipient, recording each as we go."""
    try:
        body, send_at = query("SELECT body, send_at FROM sends WHERE id = %s",
                              (send_id,), one=True)
        phones = recipients_list()
        query("UPDATE sends SET status = %s, recipients = %s WHERE id = %s",
              ("sending" if send_at is None else "scheduling", len(phones), send_id))

        extra = {}
        if send_at is not None:
            extra = {"schedule_type": "fixed", "send_at": send_at.astimezone(timezone.utc)}
        if callback_url:
            extra["status_callback"] = callback_url

        for i, phone in enumerate(phones):
            try:
                m = client.messages.create(messaging_service_sid=MESSAGING_SERVICE_SID,
                                           body=body, to=phone, **extra)
                sid, status, code = m.sid, m.status, None
            except TwilioRestException as e:
                sid, status, code = None, "failed", e.code
                if i == 0 and e.code != 21610:
                    query("INSERT INTO send_messages (send_id, phone, sid, status, error_code) "
                          "VALUES (%s, %s, %s, %s, %s)", (send_id, phone, sid, status, code))
                    query("UPDATE sends SET status = 'error', note = %s WHERE id = %s",
                          (f"Stopped: first message failed with Twilio error {e.code}: {e.msg}",
                           send_id))
                    return
            query("INSERT INTO send_messages (send_id, phone, sid, status, error_code) "
                  "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                  (send_id, phone, sid, status, code))

        query("UPDATE sends SET status = %s WHERE id = %s",
              ("sent" if send_at is None else "scheduled", send_id))
    except Exception as exc:
        print(f"[ERROR] send {send_id} crashed: {exc}")
        query("UPDATE sends SET status = 'error', note = %s WHERE id = %s",
              (f"Crashed part-way: {exc}", send_id))


def run_cancel(send_id):
    rows = query("SELECT sid FROM send_messages WHERE send_id = %s AND status = 'scheduled'",
                 (send_id,))
    failed = 0
    for (sid,) in rows:
        try:
            client.messages(sid).update(status="canceled")
            query("UPDATE send_messages SET status = 'canceled', updated_at = now() "
                  "WHERE sid = %s", (sid,))
        except TwilioRestException as e:
            failed += 1
            print(f"[ERROR] cancel {sid}: {e.msg}")
    query("UPDATE sends SET status = 'canceled', note = %s WHERE id = %s",
          (f"{failed} could not be cancelled" if failed else None, send_id))


def run_refresh(send_id):
    """Ask Twilio directly, in case some status callbacks never arrived."""
    rows = query("SELECT sid FROM send_messages WHERE send_id = %s AND sid IS NOT NULL "
                 "AND status <> ALL(%s)", (send_id, list(FINAL_STATUSES)))
    for (sid,) in rows:
        try:
            m = client.messages(sid).fetch()
            query("UPDATE send_messages SET status = %s, error_code = %s, updated_at = now() "
                  "WHERE sid = %s", (m.status, m.error_code, sid))
        except TwilioRestException as e:
            print(f"[ERROR] refresh {sid}: {e.msg}")


def start(target, *args):
    threading.Thread(target=target, args=args, daemon=True).start()


# ----------------------------
# PAGE LAYOUT
# ----------------------------
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1c1f24;--muted:#626a75;--line:#dde1e6;
--accent:#1d6f42;--accent-text:#fff;--warn:#9a3412;--warn-bg:#fff4ed;--ok-bg:#ecfdf3}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--card:#1f2228;--text:#e8eaed;
--muted:#9aa2ad;--line:#343840;--accent:#3fa86b;--accent-text:#0d1a12;--warn:#fdba74;
--warn-bg:#3a2414;--ok-bg:#15301f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:720px;margin:0 auto;padding:16px}
h1{font-size:1.4rem;margin:8px 0 16px}h2{font-size:1.1rem;margin:0 0 12px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px;margin-bottom:16px}
textarea,input[type=datetime-local]{width:100%;font:inherit;color:inherit;
background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px}
textarea{min-height:200px}
button,.button{display:inline-block;font:inherit;font-weight:600;border:0;border-radius:8px;
padding:10px 16px;background:var(--accent);color:var(--accent-text);cursor:pointer;
text-decoration:none;margin-top:8px}
button.secondary,.button.secondary{background:transparent;color:var(--text);
border:1px solid var(--line)}
button.danger{background:var(--warn);color:var(--card)}
pre.msg{white-space:pre-wrap;word-wrap:break-word;background:var(--bg);
border:1px solid var(--line);border-radius:8px;padding:12px;font:inherit;margin:0 0 12px}
.muted{color:var(--muted)}.note{background:var(--warn-bg);color:var(--warn);
border-radius:8px;padding:10px 12px;margin-bottom:12px}
.ok{background:var(--ok-bg);border-radius:8px;padding:10px 12px;margin-bottom:12px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 4px;
border-bottom:1px solid var(--line);vertical-align:top}th{font-weight:600}
.big{font-size:1.6rem;font-weight:700}label{display:block;margin:8px 0}
a{color:var(--accent)}
header{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;
gap:8px;margin-bottom:8px}header h1{margin:8px 0}
.account{display:flex;align-items:center;gap:10px;margin:0}
.account button{margin-top:0;padding:6px 12px}
input[type=text],input[type=password]{width:100%;font:inherit;color:inherit;
background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px}
"""

COUNTER_JS = """
<script>
(function(){var t=document.querySelector('textarea[name=body]');var o=document.getElementById('counter');
if(!t||!o)return;var gsm=/^[@£$¥èéùìòÇ\\nØø\\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !"#¤%&'()*+,\\-.\\/0-9:;<=>?¡A-ZÄÖÑÜ§¿a-zäöñüà^{}\\\\\\[~\\]|€]*$/;
function upd(){var v=t.value.trim();var n,parts;if(gsm.test(v)){n=v.length;parts=n<=160?1:Math.ceil(n/153);}
else{n=v.length;parts=n<=70?1:Math.ceil(n/67);}o.textContent=v.length+' characters, about '+parts+' SMS part'+(parts==1?'':'s')+' per person';}
t.addEventListener('input',upd);upd();})();
</script>
"""


def page(title, content, refresh=False):
    meta = '<meta http-equiv="refresh" content="8">' if refresh else ""
    account = ""
    if session.get("admin"):
        account = (f"<form method='post' action='/admin/logout' class='account'>"
                   f"<span class='muted'>{escape(ADMIN_USER)}</span>"
                   f"<button class='secondary' type='submit'>Log out</button></form>")
    html = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>{meta}"
            f"<title>{escape(title)}</title><style>{CSS}</style></head>"
            f"<body><main><header><h1><a href='/admin' style='color:inherit;text-decoration:none'>"
            f"Misbah SMS</a></h1>{account}</header>{content}</main></body></html>")
    return Response(html, mimetype="text/html")


def discard_form(draft_id):
    return (f"<form method='post' action='/admin/draft/{draft_id}/discard' style='display:inline'"
            f" onsubmit=\"return confirm('Cancel this message? Nothing will be sent.')\">"
            f"<button class='secondary' type='submit'>Cancel, don't send</button></form>")


def error_page(message, back="/admin"):
    return page("Problem", f"<section><div class='note'>{escape(message)}</div>"
                           f"<a class='button secondary' href='{back}'>Go back</a></section>")


# ----------------------------
# AUTH
# ----------------------------
SESSION_DAYS = 30


def same_site_post():
    """Block forms posted from other websites (the browser sends the login cookie)."""
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    return urlparse(origin).netloc == request.host


def safe_next(target):
    """Only ever send people back to a page of this admin site."""
    if target and target.startswith("/admin") and not target.startswith("//"):
        return target
    return "/admin"


def admin_required(view):
    def wrapped(*args, **kwargs):
        if not ADMIN_PASSWORD:
            return Response("Admin page is off: set ADMIN_PASSWORD in Railway.", 503)
        if not MESSAGING_SERVICE_SID:
            return Response("Set TWILIO_MESSAGING_SERVICE_SID in Railway.", 503)
        if not session.get("admin"):
            target = request.full_path if request.method == "GET" else "/admin"
            return redirect("/admin/login?next=" + quote(target.rstrip("?")), 303)
        if request.method == "POST" and not same_site_post():
            return Response("Forbidden", 403)
        return view(*args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def login_page(error="", next_url="/admin"):
    note = f"<div class='note'>{escape(error)}</div>" if error else ""
    return page("Sign in", f"""
        <section>
          <h2>Sign in</h2>{note}
          <form method="post" action="/admin/login">
            <input type="hidden" name="next" value="{escape(next_url)}">
            <label>Username
              <input type="text" name="username" autocomplete="username" autocapitalize="none"
                     autocorrect="off" spellcheck="false" required autofocus></label>
            <label>Password
              <input type="password" name="password" autocomplete="current-password" required></label>
            <button type="submit">Sign in</button>
          </form>
        </section>""")


# ----------------------------
# ROUTES
# ----------------------------
def register_admin(app):
    init_admin_db()

    # Sign-in cookie. The signing key is derived from the password and the
    # Twilio token, so changing ADMIN_PASSWORD signs everyone out.
    if not app.secret_key:
        app.secret_key = hashlib.sha256(
            ("misbah-admin-session:" + ADMIN_PASSWORD + ":"
             + os.environ["TWILIO_AUTH_TOKEN"]).encode()).hexdigest()
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
    )

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if not ADMIN_PASSWORD:
            return Response("Admin page is off: set ADMIN_PASSWORD in Railway.", 503)
        next_url = safe_next(request.values.get("next"))
        if request.method == "GET":
            if session.get("admin"):
                return redirect(next_url, 303)
            return login_page(next_url=next_url)
        if not same_site_post():
            return Response("Forbidden", 403)
        user_ok = hmac.compare_digest(
            (request.form.get("username") or "").strip().lower(), ADMIN_USER.lower())
        pass_ok = hmac.compare_digest(request.form.get("password") or "", ADMIN_PASSWORD)
        if not (user_ok and pass_ok):
            time.sleep(1)  # slow down password guessing
            return login_page("Wrong username or password.", next_url)
        session.clear()
        session.permanent = True
        session["admin"] = True
        return redirect(next_url, 303)

    @app.route("/admin/logout", methods=["POST"])
    def admin_logout():
        session.clear()
        return redirect("/admin/login", 303)

    @app.route("/sms/status", methods=["POST"])
    def sms_status():
        sid = request.form.get("MessageSid", "")
        status = request.form.get("MessageStatus", "")
        code = request.form.get("ErrorCode") or None
        if sid and status:
            # Callbacks can arrive out of order: never replace a final status
            # with an in-between one.
            query("""
                UPDATE send_messages SET status = %s, error_code = %s, updated_at = now()
                WHERE sid = %s AND (status <> ALL(%s) OR %s = ANY(%s))
            """, (status, int(code) if code else None, sid,
                  list(FINAL_STATUSES), status, list(FINAL_STATUSES)))
        return ("", 204)

    @app.route("/admin")
    @admin_required
    def admin_home():
        draft_body = ""
        if request.args.get("draft"):
            row = query("SELECT body FROM sends WHERE id = %s", (request.args["draft"],), one=True)
            draft_body = row[0] if row else ""

        subscribed, opted_out = query(
            "SELECT count(*) FILTER (WHERE opted_in), count(*) FILTER (WHERE NOT opted_in) "
            "FROM subscribers", one=True)
        testers = testers_list()
        sends = query("""
            SELECT s.id, s.created_at, s.send_at, s.status, s.recipients,
                   count(m.*) FILTER (WHERE m.status = 'delivered'),
                   left(s.body, 60)
            FROM sends s LEFT JOIN send_messages m ON m.send_id = s.id
            WHERE s.status NOT IN ('draft', 'discarded')
            GROUP BY s.id ORDER BY s.id DESC LIMIT 15
        """)

        rows = "".join(
            f"<tr><td><a href='/admin/send/{sid}'>{escape(text)}…</a><br>"
            f"<span class='muted'>{escape(fmt_time(send_at or created))}</span></td>"
            f"<td>{escape(status)}</td><td>{delivered} / {recips}</td></tr>"
            for sid, created, send_at, status, recips, delivered, text in sends
        ) or "<tr><td colspan='3' class='muted'>No sends from this page yet.</td></tr>"

        trial = ("<div class='note'>Trial mode (TESTERS_ONLY=1): every send goes to "
                 "testers only.</div>") if TESTERS_ONLY else ""

        content = f"""
        {trial}
        <section>
          <h2>New message</h2>
          <form method="post" action="/admin/sample">
            <textarea name="body" required placeholder="Type or paste the message">{escape(draft_body)}</textarea>
            <div id="counter" class="muted"></div>
            <button type="submit">Send sample to my phones</button>
          </form>
          <p class="muted">The sample goes only to the testers: {escape(', '.join(testers) or 'none set')}.
          You choose the send time after checking it.</p>
        </section>
        <section>
          <h2>Recent sends</h2>
          <table><tr><th>Message</th><th>Status</th><th>Reached</th></tr>{rows}</table>
        </section>
        <section>
          <h2>Subscribers</h2>
          <p><span class="big">{subscribed}</span> subscribed &middot;
          <span class="muted">{opted_out} opted out</span></p>
          <form method="post" action="/admin/subscribers/add">
            <label>Add phone numbers people gave you (one per line or comma-separated)
              <textarea name="numbers" style="min-height:90px" required></textarea></label>
            <label><input type="checkbox" name="welcome" value="1" checked>
              Text them a welcome message: <span class="muted">{escape(WELCOME_TEXT)}</span></label>
            <button type="submit" class="secondary">Add numbers</button>
          </form>
        </section>
        {COUNTER_JS}
        """
        return page("Misbah SMS", content)

    @app.route("/admin/sample", methods=["POST"])
    @admin_required
    def admin_sample():
        body = (request.form.get("body") or "").strip().replace("\r\n", "\n")
        if not body:
            return error_page("The message is empty.")
        testers = testers_list()
        if not testers:
            return error_page("No testers are set, so there is nobody to send the sample to.")

        draft_id = query("INSERT INTO sends (body, status) VALUES (%s, 'draft') RETURNING id",
                         (body,), one=True)[0]
        problems = []
        for phone in testers:
            try:
                client.messages.create(messaging_service_sid=MESSAGING_SERVICE_SID,
                                       body=body, to=phone)
            except TwilioRestException as e:
                problems.append(f"{phone}: Twilio error {e.code}")
        if problems:
            return error_page("The sample could not be sent to " + "; ".join(problems))
        return redirect(f"/admin/draft/{draft_id}", 303)

    @app.route("/admin/draft/<int:draft_id>")
    @admin_required
    def admin_draft(draft_id):
        row = query("SELECT body, status FROM sends WHERE id = %s", (draft_id,), one=True)
        if not row:
            return error_page("That draft does not exist.")
        body, status = row
        if status != "draft":
            return redirect(f"/admin/send/{draft_id}", 303)
        default_at = (datetime.now(ET) + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
        content = f"""
        <section>
          <div class="ok">Sample sent to your phones. Check how it looks there.</div>
          <pre class="msg">{escape(body)}</pre>
          <p class="muted">{len(body)} characters, {sms_parts(body)} SMS part(s) per person.</p>
          <a class="button secondary" href="/admin?draft={draft_id}">Change the text</a>
          {discard_form(draft_id)}
        </section>
        <section>
          <h2>When should it go out?</h2>
          <form method="post" action="/admin/draft/{draft_id}/preview">
            <label><input type="radio" name="when" value="later" checked> At this time (US Eastern):</label>
            <input type="datetime-local" name="at" value="{default_at}">
            <label><input type="radio" name="when" value="now"> Now</label>
            <button type="submit">Preview</button>
          </form>
          <p class="muted">Nothing is sent until you confirm on the next page.</p>
        </section>
        """
        return page("Choose the time", content)

    @app.route("/admin/draft/<int:draft_id>/preview", methods=["POST"])
    @admin_required
    def admin_preview(draft_id):
        row = query("SELECT body, status FROM sends WHERE id = %s", (draft_id,), one=True)
        if not row or row[1] != "draft":
            return redirect(f"/admin/send/{draft_id}", 303)
        body = row[0]
        send_at, problem = parse_when(request.form)
        if problem:
            return error_page(problem, back=f"/admin/draft/{draft_id}")
        count = len(recipients_list())
        parts = sms_parts(body)
        cost = parts * count * PRICE_PER_PART
        when_note = ("It starts right away and <b>cannot be cancelled</b>."
                     if send_at is None else "You can cancel it any time before then.")
        trial = " (trial mode: testers only)" if TESTERS_ONLY else ""
        content = f"""
        <section>
          <h2>Confirm</h2>
          <pre class="msg">{escape(body)}</pre>
          <table>
            <tr><th>When</th><td>{escape(fmt_time(send_at))}</td></tr>
            <tr><th>Recipients</th><td>{count}{trial}</td></tr>
            <tr><th>Cost</th><td>about ${cost:.2f} ({parts} part(s) each)</td></tr>
          </table>
          <p>{when_note}</p>
          <form method="post" action="/admin/draft/{draft_id}/confirm"
                onsubmit="this.querySelector('button').disabled=true">
            <input type="hidden" name="when" value="{'now' if send_at is None else 'later'}">
            <input type="hidden" name="at" value="{escape(request.form.get('at', ''))}">
            <button type="submit">{'Send now' if send_at is None else 'Schedule'} to {count} people</button>
          </form>
          <a class="button secondary" href="/admin/draft/{draft_id}">Back</a>
          {discard_form(draft_id)}
        </section>
        """
        return page("Confirm", content)

    @app.route("/admin/draft/<int:draft_id>/discard", methods=["POST"])
    @admin_required
    def admin_discard(draft_id):
        # Only a draft can be discarded; anything already sent or scheduled is untouched.
        query("UPDATE sends SET status = 'discarded' WHERE id = %s AND status = 'draft'",
              (draft_id,))
        return redirect("/admin", 303)

    @app.route("/admin/draft/<int:draft_id>/confirm", methods=["POST"])
    @admin_required
    def admin_confirm(draft_id):
        send_at, problem = parse_when(request.form)
        if problem:
            return error_page(problem, back=f"/admin/draft/{draft_id}")
        # Only one confirm can win, even with a double click.
        started = query("UPDATE sends SET status = 'starting', send_at = %s "
                        "WHERE id = %s AND status = 'draft' RETURNING id",
                        (send_at, draft_id), one=True)
        if started:
            start(run_send, draft_id, status_callback_url())
        return redirect(f"/admin/send/{draft_id}", 303)

    @app.route("/admin/send/<int:send_id>")
    @admin_required
    def admin_send(send_id):
        row = query("SELECT body, status, send_at, recipients, note, created_at "
                    "FROM sends WHERE id = %s", (send_id,), one=True)
        if not row:
            return error_page("That send does not exist.")
        body, status, send_at, recipients, note, created = row
        if status == "draft":
            return redirect(f"/admin/draft/{send_id}", 303)

        counts = dict(query("SELECT status, count(*) FROM send_messages WHERE send_id = %s "
                            "GROUP BY status", (send_id,)))
        errors = query("SELECT error_code, count(*) FROM send_messages WHERE send_id = %s "
                       "AND error_code IS NOT NULL GROUP BY error_code ORDER BY 2 DESC",
                       (send_id,))
        done = sum(counts.values())
        delivered = counts.get("delivered", 0)
        busy = status in ("starting", "scheduling", "sending")

        progress = (f"<div class='note'>Working: {done} of {recipients or '…'} "
                    f"handed to Twilio. This page refreshes itself.</div>") if busy else ""
        note_html = f"<div class='note'>{escape(note)}</div>" if note else ""
        status_rows = "".join(f"<tr><td>{escape(s)}</td><td>{n}</td></tr>"
                              for s, n in sorted(counts.items(), key=lambda x: -x[1]))
        error_rows = "".join(
            f"<tr><td>{code}</td><td>{escape(ERROR_MEANINGS.get(code, 'see Twilio console'))}</td>"
            f"<td>{n}</td></tr>" for code, n in errors)

        can_cancel = (status == "scheduled" and send_at
                      and send_at > datetime.now(timezone.utc) and counts.get("scheduled"))
        cancel_form = f"""
          <form method="post" action="/admin/send/{send_id}/cancel"
                onsubmit="return confirm('Cancel this send for everyone?')">
            <button class="danger" type="submit">Cancel this send</button>
          </form>""" if can_cancel else ""
        refresh_form = f"""
          <form method="post" action="/admin/send/{send_id}/refresh">
            <button class="secondary" type="submit">Re-check with Twilio</button>
          </form>""" if not busy and done else ""

        content = f"""
        <section>
          {progress}{note_html}
          <p><span class="big">{delivered} of {recipients}</span> reached
             <span class="muted">(delivered)</span></p>
          <table>
            <tr><th>Status</th><td>{escape(status)}</td></tr>
            <tr><th>Send time</th><td>{escape(fmt_time(send_at) if send_at else 'right away, ' + fmt_time(created))}</td></tr>
          </table>
        </section>
        <section>
          <h2>Messages by status</h2>
          <table>{status_rows or "<tr><td class='muted'>None yet</td></tr>"}</table>
          {"<h2 style='margin-top:16px'>Why some failed</h2><table><tr><th>Error</th><th>Meaning</th><th>Count</th></tr>" + error_rows + "</table>" if error_rows else ""}
          {refresh_form}{cancel_form}
        </section>
        <section>
          <h2>Message</h2>
          <pre class="msg">{escape(body)}</pre>
        </section>
        """
        return page(f"Send {send_id}", content, refresh=busy)

    @app.route("/admin/send/<int:send_id>/cancel", methods=["POST"])
    @admin_required
    def admin_cancel(send_id):
        claimed = query("UPDATE sends SET status = 'canceling' WHERE id = %s "
                        "AND status = 'scheduled' RETURNING id", (send_id,), one=True)
        if claimed:
            start(run_cancel, send_id)
        return redirect(f"/admin/send/{send_id}", 303)

    @app.route("/admin/send/<int:send_id>/refresh", methods=["POST"])
    @admin_required
    def admin_refresh(send_id):
        start(run_refresh, send_id)
        return redirect(f"/admin/send/{send_id}", 303)

    @app.route("/admin/subscribers/add", methods=["POST"])
    @admin_required
    def admin_add_numbers():
        raw = (request.form.get("numbers") or "").replace(",", "\n").splitlines()
        welcome = request.form.get("welcome") == "1"
        lines = []
        seen = set()
        for item in (r.strip() for r in raw):
            if not item:
                continue
            phone = to_e164(item)
            if phone is None:
                lines.append(f"⚠️ {escape(item)}: not a valid number, skipped")
                continue
            if phone in seen:
                continue
            seen.add(phone)
            row = query("SELECT opted_in FROM subscribers WHERE phone_e164 = %s",
                        (phone,), one=True)
            if row and row[0]:
                lines.append(f"✔️ {phone}: already subscribed")
            elif row:
                lines.append(f"🚫 {phone}: replied STOP earlier, NOT added "
                             "(they can text JOIN or START themselves)")
            else:
                query("INSERT INTO subscribers (phone_e164, opted_in, source, updated_at) "
                      "VALUES (%s, TRUE, 'admin_page', now()) ON CONFLICT DO NOTHING", (phone,))
                text = f"✅ {phone}: added"
                if welcome:
                    try:
                        client.messages.create(messaging_service_sid=MESSAGING_SERVICE_SID,
                                               body=WELCOME_TEXT, to=phone)
                        text += ", welcome sent"
                    except TwilioRestException as e:
                        text += f", welcome failed (Twilio error {e.code})"
                lines.append(text)
        items = "".join(f"<li>{line}</li>" for line in lines) or "<li>No numbers given.</li>"
        return page("Numbers added", f"<section><h2>Result</h2><ul>{items}</ul>"
                                     f"<a class='button secondary' href='/admin'>Back</a></section>")

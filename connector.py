# connector.py
#
# Lets Claude (phone app, claude.ai, desktop app, Claude Code) send the
# reminders through this app, as a "custom connector" (MCP server).
#
#   /mcp                                   the connector Claude talks to
#   /.well-known/oauth-protected-resource  tells Claude how to sign in
#   /.well-known/oauth-authorization-server
#   /oauth/register, /oauth/authorize, /oauth/token
#
# Signing in: when you add the connector, Claude opens /oauth/authorize. You
# sign in with the admin username and password and press Allow. Claude then
# gets a token that expires after an hour and is renewed automatically. Every
# connected Claude account is listed on the admin page with a Disconnect button.

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote, urlencode, urlparse

from flask import Response, current_app, jsonify, redirect, request, session
from twilio.base.exceptions import TwilioRestException

import admin as A

ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=90)
CODE_TTL = timedelta(minutes=5)
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1")
SCOPES = ["sms", "offline_access"]
PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = """\
This connector sends SMS reminders to the Misbah event-reminder subscribers.
Messages reach real people and cost money, so always follow this order:
1. send_sample with the user's text exactly as given (never reword, translate or
   "fix" it, including emoji, Arabic and blank lines). It texts only the user's
   own tester phones. Then stop and ask the user to check their phones.
2. Only after the user clearly says the sample looks good, ask when to send.
   Times are US Eastern. Call preview_send and show the user the time (ET and
   CT), recipient count and cost. Stop and wait for their explicit OK.
3. Only after that OK, call schedule_send with the confirmation_code from
   preview_send. "now" cannot be cancelled; scheduled sends can.
If the user wants changes, call send_sample again with the new text (a new
draft); do not reuse the old draft. Use discard_draft for drafts the user drops.
Cancel a scheduled send only when the user asks. Report reach with
get_send_status after the send time."""


# ----------------------------
# STORAGE
# ----------------------------
def init_connector_db():
    with A.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    client_name TEXT NOT NULL,
                    redirect_uris TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_used_at TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    code_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    scope TEXT,
                    expires_at TIMESTAMPTZ NOT NULL,
                    used BOOLEAN NOT NULL DEFAULT FALSE
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    token_hash TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    scope TEXT,
                    expires_at TIMESTAMPTZ NOT NULL,
                    revoked BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
        conn.commit()


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def base_url():
    return "https://" + request.host


def now():
    return datetime.now(timezone.utc)


# ----------------------------
# OAUTH HELPERS
# ----------------------------
def redirect_allowed(uri):
    """Only Claude's own return addresses can be registered."""
    if uri == CLAUDE_CALLBACK:
        return True
    p = urlparse(uri)
    return (p.scheme == "http" and p.hostname in LOOPBACK_HOSTS
            and p.path == "/callback" and not p.query and not p.fragment)


def redirect_matches(registered, uri):
    for r in registered:
        if r == uri:
            return True
        pr, pu = urlparse(r), urlparse(uri)
        # Claude Code uses a different local port each session.
        if (pr.scheme == pu.scheme == "http" and pr.hostname in LOOPBACK_HOSTS
                and pu.hostname == pr.hostname and pu.path == pr.path
                and not pu.query and not pu.fragment):
            return True
    return False


def get_client(client_id):
    row = A.query("SELECT client_name, redirect_uris FROM oauth_clients WHERE client_id = %s",
                  (client_id or "",), one=True)
    if not row:
        return None
    return {"name": row[0], "redirect_uris": json.loads(row[1])}


def issue_tokens(client_id, scope):
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    for token, kind, ttl in ((access, "access", ACCESS_TTL), (refresh, "refresh", REFRESH_TTL)):
        A.query("INSERT INTO oauth_tokens (token_hash, kind, client_id, scope, expires_at) "
                "VALUES (%s, %s, %s, %s, %s)", (digest(token), kind, client_id, scope, now() + ttl))
    resp = jsonify(access_token=access, token_type="Bearer",
                   expires_in=int(ACCESS_TTL.total_seconds()),
                   refresh_token=refresh, scope=scope or "sms")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def token_error(error, description=""):
    resp = jsonify(error=error, error_description=description)
    resp.status_code = 400
    resp.headers["Cache-Control"] = "no-store"
    return resp


def with_query(uri, params):
    return uri + ("&" if "?" in uri else "?") + urlencode(params)


def authorized_client():
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    row = A.query("SELECT client_id FROM oauth_tokens WHERE token_hash = %s AND kind = 'access' "
                  "AND NOT revoked AND expires_at > now()", (digest(auth[7:].strip()),), one=True)
    if row:
        A.query("UPDATE oauth_clients SET last_used_at = now() WHERE client_id = %s", (row[0],))
    return row[0] if row else None


def unauthorized():
    return Response(
        json.dumps({"error": "unauthorized"}), 401, mimetype="application/json",
        headers={"WWW-Authenticate":
                 f'Bearer resource_metadata="{base_url()}/.well-known/oauth-protected-resource"'})


# ----------------------------
# TOOLS
# ----------------------------
class ToolError(Exception):
    pass


def int_arg(args, name):
    try:
        return int(args[name])
    except (KeyError, TypeError, ValueError):
        raise ToolError(f"'{name}' must be a number.")


def parse_when_arg(when):
    when = (when or "").strip()
    form = {"when": "now"} if when.lower() == "now" else {"when": "later", "at": when}
    send_at, problem = A.parse_when(form)
    if problem:
        raise ToolError(problem + ' Use "now" or a US Eastern time as "YYYY-MM-DD HH:MM".')
    return send_at


def confirmation_code(draft_id, send_at):
    key = "now" if send_at is None else send_at.astimezone(timezone.utc).isoformat()
    mac = hmac.new(current_app.secret_key.encode(), f"{draft_id}|{key}".encode(), hashlib.sha256)
    return mac.hexdigest()[:10]


def draft_body(draft_id):
    row = A.query("SELECT body, status FROM sends WHERE id = %s", (draft_id,), one=True)
    if not row:
        raise ToolError(f"There is no draft {draft_id}.")
    if row[1] != "draft":
        raise ToolError(f"Draft {draft_id} is no longer a draft (status: {row[1]}). "
                        "Start again with send_sample.")
    return row[0]


def tool_get_overview(args):
    subscribed, opted_out = A.query(
        "SELECT count(*) FILTER (WHERE opted_in), count(*) FILTER (WHERE NOT opted_in) "
        "FROM subscribers", one=True)
    lines = [f"Subscribed: {subscribed}", f"Opted out: {opted_out}",
             f"Testers (get samples): {', '.join(A.testers_list()) or 'none'}"]
    if A.TESTERS_ONLY:
        lines.append("Trial mode is on: every send goes to testers only.")
    lines.append("Admin page: " + base_url() + "/admin")
    return "\n".join(lines)


def tool_send_sample(args):
    body = (args.get("text") or "").strip().replace("\r\n", "\n")
    if not body:
        raise ToolError("The message text is empty.")
    testers = A.testers_list()
    if not testers:
        raise ToolError("No testers are set, so there is nobody to send the sample to.")
    draft_id = A.query("INSERT INTO sends (body, status) VALUES (%s, 'draft') RETURNING id",
                       (body,), one=True)[0]
    problems = []
    for phone in testers:
        try:
            A.client.messages.create(messaging_service_sid=A.MESSAGING_SERVICE_SID,
                                     body=body, to=phone)
        except TwilioRestException as e:
            problems.append(f"{phone}: Twilio error {e.code}")
    if problems:
        raise ToolError("The sample could not be sent to " + "; ".join(problems))
    return (f"Sample sent to the testers' phones ({', '.join(testers)}).\n"
            f"draft_id: {draft_id}\n"
            f"Length: {len(body)} characters, {A.sms_parts(body)} SMS part(s) per person.\n"
            "Nothing has gone to subscribers. Ask the user to check their phones and say "
            "whether it looks good before choosing a send time.")


def tool_preview_send(args):
    draft_id = int_arg(args, "draft_id")
    body = draft_body(draft_id)
    send_at = parse_when_arg(args.get("when"))
    count = len(A.recipients_list())
    parts = A.sms_parts(body)
    cost = parts * count * A.PRICE_PER_PART
    lines = [
        "PREVIEW ONLY: nothing has been sent or scheduled.",
        f"draft_id: {draft_id}",
        f"When: {A.fmt_time(send_at)}",
        f"Recipients: {count}" + (" (trial mode: testers only)" if A.TESTERS_ONLY else ""),
        f"Cost: about ${cost:.2f} ({parts} part(s) each)",
        ("Sending now starts right away and CANNOT be cancelled." if send_at is None
         else "A scheduled send can be cancelled before its time."),
        f"confirmation_code: {confirmation_code(draft_id, send_at)}",
        "Show the user these details and wait for their explicit OK before calling "
        "schedule_send with the same draft_id, when and confirmation_code.",
    ]
    return "\n".join(lines)


def tool_schedule_send(args):
    draft_id = int_arg(args, "draft_id")
    draft_body(draft_id)
    send_at = parse_when_arg(args.get("when"))
    given = str(args.get("confirmation_code") or "")
    if not hmac.compare_digest(given, confirmation_code(draft_id, send_at)):
        raise ToolError("The confirmation_code does not match this draft and time. "
                        "Call preview_send again and get the user's OK on the new preview.")
    started = A.query("UPDATE sends SET status = 'starting', send_at = %s "
                      "WHERE id = %s AND status = 'draft' RETURNING id",
                      (send_at, draft_id), one=True)
    if not started:
        raise ToolError("This draft was already started or cancelled.")
    A.start(A.run_send, draft_id, A.status_callback_url())
    return (f"Started. send_id: {draft_id}\n"
            f"{'Sending now' if send_at is None else 'Scheduling for ' + A.fmt_time(send_at)} "
            f"to {len(A.recipients_list())} people. Handing everyone to Twilio takes about a "
            "minute; call get_send_status to see progress.\n"
            f"Page: {base_url()}/admin/send/{draft_id}")


def tool_get_send_status(args):
    send_id = int_arg(args, "send_id")
    row = A.query("SELECT body, status, send_at, recipients, note, created_at FROM sends "
                  "WHERE id = %s", (send_id,), one=True)
    if not row:
        raise ToolError(f"There is no send {send_id}.")
    body, status, send_at, recipients, note, created = row
    if args.get("recheck_with_twilio") and status not in ("draft", "discarded"):
        A.start(A.run_refresh, send_id)
    counts = dict(A.query("SELECT status, count(*) FROM send_messages WHERE send_id = %s "
                          "GROUP BY status", (send_id,)))
    errors = A.query("SELECT error_code, count(*) FROM send_messages WHERE send_id = %s "
                     "AND error_code IS NOT NULL GROUP BY error_code ORDER BY 2 DESC", (send_id,))
    lines = [f"send_id: {send_id}", f"Status: {status}",
             f"Send time: {A.fmt_time(send_at) if send_at else 'right away (' + A.fmt_time(created) + ')'}",
             f"Reached (delivered): {counts.get('delivered', 0)} of {recipients}"]
    if counts:
        lines.append("By status: " + ", ".join(f"{s} {n}" for s, n in
                                               sorted(counts.items(), key=lambda x: -x[1])))
    for code, n in errors:
        lines.append(f"Error {code} x{n}: {A.ERROR_MEANINGS.get(code, 'see Twilio console')}")
    if note:
        lines.append(f"Note: {note}")
    if args.get("recheck_with_twilio"):
        lines.append("Re-checking with Twilio in the background; ask again in a minute.")
    lines.append(f"Message: {body}")
    lines.append(f"Page: {base_url()}/admin/send/{send_id}")
    return "\n".join(lines)


def tool_list_recent_sends(args):
    limit = max(1, min(int(args.get("limit") or 10), 30))
    rows = A.query("""
        SELECT s.id, s.created_at, s.send_at, s.status, s.recipients,
               count(m.*) FILTER (WHERE m.status = 'delivered'), left(s.body, 70)
        FROM sends s LEFT JOIN send_messages m ON m.send_id = s.id
        WHERE s.status NOT IN ('draft', 'discarded')
        GROUP BY s.id ORDER BY s.id DESC LIMIT %s
    """, (limit,))
    if not rows:
        return "No sends yet from the page or this connector."
    return "\n".join(
        f"send_id {sid} | {status} | {A.fmt_time(send_at or created)} | "
        f"reached {delivered}/{recips} | {text.replace(chr(10), ' ')}"
        for sid, created, send_at, status, recips, delivered, text in rows)


def tool_cancel_scheduled_send(args):
    send_id = int_arg(args, "send_id")
    claimed = A.query("UPDATE sends SET status = 'canceling' WHERE id = %s "
                      "AND status = 'scheduled' AND send_at > now() RETURNING id",
                      (send_id,), one=True)
    if not claimed:
        row = A.query("SELECT status FROM sends WHERE id = %s", (send_id,), one=True)
        raise ToolError(f"Send {send_id} can't be cancelled (status: {row[0] if row else 'not found'}). "
                        "Only scheduled sends whose time hasn't come can be cancelled.")
    A.start(A.run_cancel, send_id)
    return (f"Cancelling send {send_id} for everyone. It takes a few seconds; "
            "call get_send_status to confirm it shows 'canceled'.")


def tool_discard_draft(args):
    draft_id = int_arg(args, "draft_id")
    done = A.query("UPDATE sends SET status = 'discarded' WHERE id = %s AND status = 'draft' "
                   "RETURNING id", (draft_id,), one=True)
    if not done:
        raise ToolError(f"Draft {draft_id} is not an unsent draft, so there is nothing to discard.")
    return f"Draft {draft_id} discarded. Nothing was sent to subscribers."


def tool_check_numbers(args):
    lines = A.add_numbers(args.get("numbers") or "", welcome=False, source="", dry_run=True)
    return "\n".join(lines) or "No numbers given."


def tool_add_subscribers(args):
    lines = A.add_numbers(args.get("numbers") or "", welcome=bool(args.get("send_welcome")),
                          source="claude_connector")
    return "\n".join(lines) or "No numbers given."


NUMBERS = {"type": "string",
           "description": "Phone numbers, one per line or comma-separated, any common US format."}
TOOLS = [
    ("get_overview", "Subscriber counts, tester numbers and trial-mode status.",
     {}, [], {"readOnlyHint": True}, tool_get_overview),
    ("send_sample", "Step 1. Save the message as a draft and text it ONLY to the user's tester "
     "phones. Pass the user's text exactly as given. Returns a draft_id. Then ask the user to "
     "check their phones.",
     {"text": {"type": "string", "description": "The message, exactly as the user gave it."}},
     ["text"], {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
     tool_send_sample),
    ("preview_send", "Step 2, after the user approved the sample. Shows when it would go out "
     "(ET and CT), how many people and the cost, and returns a confirmation_code. Sends nothing.",
     {"draft_id": {"type": "integer"},
      "when": {"type": "string",
               "description": 'Either "now" or a US Eastern date and time as "YYYY-MM-DD HH:MM" '
                              "(24-hour), at least 20 minutes and at most 35 days ahead."}},
     ["draft_id", "when"], {"readOnlyHint": True}, tool_preview_send),
    ("schedule_send", "Step 3. Schedules (or, with when=\"now\", immediately sends) the draft to "
     "ALL subscribers. Call only after the user explicitly approved the preview_send details in "
     "this conversation. Needs the confirmation_code from preview_send.",
     {"draft_id": {"type": "integer"},
      "when": {"type": "string", "description": "Exactly the same value used in preview_send."},
      "confirmation_code": {"type": "string"}},
     ["draft_id", "when", "confirmation_code"],
     {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}, tool_schedule_send),
    ("get_send_status", "Progress and reach of a send: how many were delivered, failed and why.",
     {"send_id": {"type": "integer"},
      "recheck_with_twilio": {"type": "boolean",
                              "description": "Ask Twilio again for messages whose status looks stuck."}},
     ["send_id"], {"readOnlyHint": True}, tool_get_send_status),
    ("list_recent_sends", "Recent sends with status and reach.",
     {"limit": {"type": "integer", "description": "How many (default 10, max 30)."}},
     [], {"readOnlyHint": True}, tool_list_recent_sends),
    ("cancel_scheduled_send", "Cancel a scheduled send for everyone. Only when the user asks.",
     {"send_id": {"type": "integer"}}, ["send_id"],
     {"readOnlyHint": False, "destructiveHint": True}, tool_cancel_scheduled_send),
    ("discard_draft", "Drop an unsent draft (for example after the user changed the text).",
     {"draft_id": {"type": "integer"}}, ["draft_id"],
     {"readOnlyHint": False, "destructiveHint": False}, tool_discard_draft),
    ("check_numbers", "Check phone numbers before adding: new, already subscribed, opted out "
     "or invalid. Changes nothing.", {"numbers": NUMBERS}, ["numbers"],
     {"readOnlyHint": True}, tool_check_numbers),
    ("add_subscribers", "Add phone numbers people gave the user. Never re-adds people who "
     "replied STOP. Ask the user first whether to text new numbers the welcome message.",
     {"numbers": NUMBERS,
      "send_welcome": {"type": "boolean", "description": "Text new numbers: " + A.WELCOME_TEXT}},
     ["numbers", "send_welcome"], {"readOnlyHint": False, "destructiveHint": False,
                                   "openWorldHint": True}, tool_add_subscribers),
]
TOOL_FUNCS = {name: func for name, _, _, _, _, func in TOOLS}


def tools_list():
    return [{"name": name, "description": desc,
             "inputSchema": {"type": "object", "properties": props, "required": required},
             "annotations": annotations}
            for name, desc, props, required, annotations, _ in TOOLS]


def handle_rpc(msg):
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None  # a notification, no reply
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[1],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "misbah-sms", "title": "Misbah SMS", "version": "1.0.0"},
                  "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools_list()}
    elif method == "tools/call":
        func = TOOL_FUNCS.get(params.get("name"))
        if func is None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"}}
        try:
            text, is_error = func(params.get("arguments") or {}), False
        except ToolError as e:
            text, is_error = str(e), True
        except Exception as e:  # keep the connector answering
            print(f"[ERROR] tool {params.get('name')}: {e}")
            text, is_error = f"Something went wrong on the server: {e}", True
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


# ----------------------------
# ROUTES
# ----------------------------
def register_connector(app):
    init_connector_db()

    @app.route("/.well-known/oauth-protected-resource")
    @app.route("/.well-known/oauth-protected-resource/mcp")
    def protected_resource_metadata():
        return jsonify(resource=base_url() + "/mcp", authorization_servers=[base_url()],
                       scopes_supported=SCOPES, bearer_methods_supported=["header"],
                       resource_name="Misbah SMS")

    @app.route("/.well-known/oauth-authorization-server")
    def authorization_server_metadata():
        return jsonify(issuer=base_url(),
                       authorization_endpoint=base_url() + "/oauth/authorize",
                       token_endpoint=base_url() + "/oauth/token",
                       registration_endpoint=base_url() + "/oauth/register",
                       response_types_supported=["code"],
                       grant_types_supported=["authorization_code", "refresh_token"],
                       code_challenge_methods_supported=["S256"],
                       token_endpoint_auth_methods_supported=["none"],
                       scopes_supported=SCOPES)

    @app.route("/oauth/register", methods=["POST"])
    def oauth_register():
        data = request.get_json(silent=True) or {}
        uris = data.get("redirect_uris") or []
        if (not isinstance(uris, list) or not uris
                or not all(isinstance(u, str) and redirect_allowed(u) for u in uris)):
            return jsonify(error="invalid_redirect_uri",
                           error_description="Only Claude's return addresses are accepted."), 400
        client_id = "claude_" + secrets.token_urlsafe(24)
        name = str(data.get("client_name") or "Claude")[:100]
        A.query("INSERT INTO oauth_clients (client_id, client_name, redirect_uris) "
                "VALUES (%s, %s, %s)", (client_id, name, json.dumps(uris)))
        return jsonify(client_id=client_id, client_id_issued_at=int(time.time()),
                       client_name=name, redirect_uris=uris,
                       grant_types=["authorization_code", "refresh_token"],
                       response_types=["code"], token_endpoint_auth_method="none"), 201

    @app.route("/oauth/authorize", methods=["GET", "POST"])
    def oauth_authorize():
        if not A.ADMIN_PASSWORD:
            return Response("Admin page is off: set ADMIN_PASSWORD in Railway.", 503)
        p = request.values
        client = get_client(p.get("client_id"))
        redirect_uri = p.get("redirect_uri") or ""
        if not client or not redirect_matches(client["redirect_uris"], redirect_uri):
            # Never redirect to an address we can't vouch for.
            return A.error_page("This connection request is not valid. Start again from Claude.")
        state = p.get("state")

        def back(params):
            if state is not None:
                params["state"] = state
            return redirect(with_query(redirect_uri, params), 302)

        if p.get("response_type") != "code":
            return back({"error": "unsupported_response_type"})
        if not p.get("code_challenge") or p.get("code_challenge_method") != "S256":
            return back({"error": "invalid_request", "error_description": "PKCE S256 required"})

        if not session.get("admin"):
            return redirect("/admin/login?next=" + quote(request.full_path.rstrip("?"), safe=""), 303)

        if request.method == "GET":
            hidden = "".join(
                f"<input type='hidden' name='{k}' value='{escape(p.get(k) or '')}'>"
                for k in ("client_id", "redirect_uri", "state", "code_challenge",
                          "code_challenge_method", "response_type", "scope", "resource"))
            host = urlparse(redirect_uri).hostname
            return A.page("Allow Claude?", f"""
            <section>
              <h2>Allow {escape(client['name'])} to use Misbah SMS?</h2>
              <p>It will be able to text samples to your phones, schedule or send messages to all
                 subscribers, cancel scheduled sends, check results, and add numbers.</p>
              <p class="muted">After you allow it, you return to <b>{escape(host or '')}</b>.
                 You can disconnect it any time on the admin page.</p>
              <form method="post" action="/oauth/authorize">{hidden}
                <button type="submit" name="decision" value="allow">Allow</button>
                <button class="secondary" type="submit" name="decision" value="deny">Deny</button>
              </form>
            </section>""")

        if not A.same_site_post():
            return Response("Forbidden", 403)
        if p.get("decision") != "allow":
            return back({"error": "access_denied"})
        code = secrets.token_urlsafe(32)
        A.query("INSERT INTO oauth_codes (code_hash, client_id, redirect_uri, code_challenge, "
                "scope, expires_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (digest(code), p.get("client_id"), redirect_uri, p.get("code_challenge"),
                 p.get("scope") or "sms", now() + CODE_TTL))
        return back({"code": code})

    @app.route("/oauth/token", methods=["POST"])
    def oauth_token():
        f = request.form
        grant = f.get("grant_type")
        client_id = f.get("client_id") or ""
        if not get_client(client_id):
            return token_error("invalid_client", "Unknown client")
        if grant == "authorization_code":
            row = A.query("""
                UPDATE oauth_codes SET used = TRUE
                WHERE code_hash = %s AND NOT used AND expires_at > now()
                RETURNING client_id, redirect_uri, code_challenge, scope
            """, (digest(f.get("code") or ""),), one=True)
            if not row or row[0] != client_id or row[1] != (f.get("redirect_uri") or ""):
                return token_error("invalid_grant", "Code is invalid, used or expired")
            verifier = f.get("code_verifier") or ""
            expected = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if not verifier or not hmac.compare_digest(expected, row[2]):
                return token_error("invalid_grant", "PKCE check failed")
            return issue_tokens(client_id, row[3])
        if grant == "refresh_token":
            row = A.query("""
                UPDATE oauth_tokens SET revoked = TRUE
                WHERE token_hash = %s AND kind = 'refresh' AND NOT revoked AND expires_at > now()
                RETURNING client_id, scope
            """, (digest(f.get("refresh_token") or ""),), one=True)
            if not row or row[0] != client_id:
                return token_error("invalid_grant", "Refresh token is invalid or expired")
            return issue_tokens(client_id, row[1])
        return token_error("unsupported_grant_type")

    @app.route("/mcp", methods=["GET", "POST", "DELETE"])
    def mcp():
        if request.method != "POST":
            return Response(status=405, headers={"Allow": "POST"})
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc not in (request.host, "claude.ai"):
            return Response("Forbidden origin", 403)
        if not authorized_client():
            return unauthorized()
        if not A.MESSAGING_SERVICE_SID:
            return Response("Set TWILIO_MESSAGING_SERVICE_SID in Railway.", 503)
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify(jsonrpc="2.0", id=None, error={"code": -32700, "message": "Parse error"})
        if isinstance(payload, list):
            replies = [r for r in (handle_rpc(m) for m in payload) if r]
            return jsonify(replies) if replies else ("", 202)
        reply = handle_rpc(payload)
        return jsonify(reply) if reply else ("", 202)

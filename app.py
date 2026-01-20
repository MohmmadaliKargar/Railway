import os
from datetime import datetime
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import psycopg2

app = Flask(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    phone_e164 TEXT PRIMARY KEY,
                    opted_in BOOLEAN NOT NULL DEFAULT TRUE,
                    source TEXT,
                    updated_at TIMESTAMPTZ NOT NULL
                )
            """)
        conn.commit()

def upsert_subscriber(phone_e164: str, opted_in: bool, source: str = "sms_keyword"):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO subscribers (phone_e164, opted_in, source, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (phone_e164) DO UPDATE SET
                    opted_in = EXCLUDED.opted_in,
                    source = EXCLUDED.source,
                    updated_at = EXCLUDED.updated_at
            """, (phone_e164, opted_in, source, datetime.utcnow()))
        conn.commit()

@app.route("/sms/inbound", methods=["POST"])
def inbound_sms():
    from_number = request.form.get("From", "")  # already E.164
    body = (request.form.get("Body", "") or "").strip().lower()

    resp = MessagingResponse()

    if body == "join":
        upsert_subscriber(from_number, True)
        resp.message("✅ You’re subscribed. Reply STOP to opt out.")
    elif body in ("stop", "unsubscribe", "cancel", "end", "quit"):
        upsert_subscriber(from_number, False)
        resp.message("You’re unsubscribed. Reply START to re-subscribe.")
    elif body == "start":
        upsert_subscriber(from_number, True)
        resp.message("Welcome back! You’re subscribed again. Reply STOP to opt out.")
    elif body == "help":
        resp.message("Reply JOIN to subscribe. Reply STOP to opt out.")
    else:
        resp.message("Reply JOIN to subscribe. Reply HELP for options.")

    return str(resp)

# Create table on startup
init_db()

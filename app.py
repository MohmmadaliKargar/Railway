import os
from datetime import datetime
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import psycopg2

app = Flask(__name__)

# ----------------------------
# ENV VARS
# ----------------------------
DATABASE_URL = os.environ["DATABASE_URL"]

# Needed to send you alert SMS
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM_NUMBER = os.environ["TWILIO_FROM_NUMBER"]  # your Twilio number, e.g. +1888...
ADMIN_PHONE = os.environ["ADMIN_PHONE"]                # your personal phone, e.g. +1312...

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ----------------------------
# DB HELPERS
# ----------------------------
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

# ----------------------------
# WEBHOOK
# ----------------------------
@app.route("/sms/inbound", methods=["POST"])
def inbound_sms():
    from_number = request.form.get("From", "")  # already E.164
    original_body = request.form.get("Body", "") or ""
    body = original_body.strip().lower()

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
        # Alert YOU with the incoming message
        try:
            twilio_client.messages.create(
                from_=TWILIO_FROM_NUMBER,
                to=ADMIN_PHONE,
                body=(
                    "New inbound SMS\n"
                    f"From: {from_number}\n"
                    f"Message: {original_body}"
                ),
            )
        except Exception as exc:
            # Don't break the webhook if alert fails
            print(f"[ERROR] Failed to alert admin: {exc}")

        # Reply to the sender
        resp.message(
            "Your message has been received. We will get back to you shortly.\n\n"
            "Reply JOIN to subscribe. Reply HELP for options."
        )

    return str(resp)

# Create table on startup
init_db()

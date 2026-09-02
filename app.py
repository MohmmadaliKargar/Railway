import os
import re
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
TWILIO_FROM_NUMBER = os.environ["TWILIO_FROM_NUMBER"]
ADMIN_PHONE = os.environ["ADMIN_PHONE"]

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# ----------------------------
# DB HELPERS
# ----------------------------
def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:

            # Create the table if this is a brand-new database.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    phone_e164 TEXT PRIMARY KEY,
                    opted_in BOOLEAN NOT NULL DEFAULT TRUE,
                    source TEXT,
                    updated_at TIMESTAMPTZ NOT NULL
                )
            """)

            # Add email support to an EXISTING table.
            # These commands do NOT delete existing subscribers.
            cur.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS email TEXT
            """)

            cur.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS awaiting_email BOOLEAN
                NOT NULL DEFAULT FALSE
            """)

        conn.commit()


def upsert_subscriber(
    phone_e164: str,
    opted_in: bool,
    source: str = "sms_keyword"
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO subscribers (
                    phone_e164,
                    opted_in,
                    source,
                    updated_at
                )
                VALUES (%s, %s, %s, %s)

                ON CONFLICT (phone_e164) DO UPDATE SET
                    opted_in = EXCLUDED.opted_in,
                    source = EXCLUDED.source,
                    updated_at = EXCLUDED.updated_at
            """, (
                phone_e164,
                opted_in,
                source,
                datetime.utcnow()
            ))

        conn.commit()


def set_awaiting_email(phone_e164: str, awaiting: bool):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE subscribers
                SET awaiting_email = %s,
                    updated_at = %s
                WHERE phone_e164 = %s
            """, (
                awaiting,
                datetime.utcnow(),
                phone_e164
            ))

        conn.commit()


def is_awaiting_email(phone_e164: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT awaiting_email
                FROM subscribers
                WHERE phone_e164 = %s
            """, (phone_e164,))

            row = cur.fetchone()

    return bool(row and row[0])


def save_email(phone_e164: str, email: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE subscribers
                SET email = %s,
                    awaiting_email = FALSE,
                    updated_at = %s
                WHERE phone_e164 = %s
            """, (
                email,
                datetime.utcnow(),
                phone_e164
            ))

        conn.commit()


def is_valid_email(value: str) -> bool:
    # Simple practical email validation
    return bool(
        re.fullmatch(
            r"[^@\s]+@[^@\s]+\.[^@\s]+",
            value
        )
    )


# ----------------------------
# WEBHOOK
# ----------------------------
@app.route("/sms/inbound", methods=["POST"])
def inbound_sms():

    from_number = request.form.get("From", "")
    original_body = request.form.get("Body", "") or ""

    body = original_body.strip()
    body_lower = body.lower()

    resp = MessagingResponse()

    # ------------------------
    # JOIN
    # ------------------------
    if body_lower == "join":

        # Subscribe immediately.
        # Email is OPTIONAL.
        upsert_subscriber(from_number, True)

        # The next message can be treated as an email address.
        set_awaiting_email(from_number, True)

        resp.message(
            "✅ You’re subscribed. Please provide your email address. "
            "Reply STOP to opt out."
        )

    # ------------------------
    # STOP
    # ------------------------
    elif body_lower in (
        "stop",
        "unsubscribe",
        "cancel",
        "end",
        "quit"
    ):

        upsert_subscriber(from_number, False)
        set_awaiting_email(from_number, False)

        resp.message(
            "You’re unsubscribed. Reply START to re-subscribe."
        )

    # ------------------------
    # START
    # ------------------------
    elif body_lower == "start":

        upsert_subscriber(from_number, True)

        resp.message(
            "Welcome back! You’re subscribed again. "
            "Reply STOP to opt out."
        )

    # ------------------------
    # HELP
    # ------------------------
    elif body_lower == "help":

        resp.message(
            "Reply JOIN to subscribe. Reply STOP to opt out."
        )

    # ------------------------
    # EMAIL RESPONSE
    # ------------------------
    elif is_awaiting_email(from_number):

        if is_valid_email(body):

            save_email(from_number, body)

            resp.message(
                "Thank you! Your email address has been saved."
            )

        else:

            # They are already subscribed.
            # We simply tell them that the email is optional.
            resp.message(
                "That doesn’t appear to be a valid email address. "
                "Please reply with your email address, or ignore this "
                "message if you prefer SMS reminders only."
            )

    # ------------------------
    # OTHER MESSAGES
    # ------------------------
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
            "Your message has been received. "
            "We will get back to you shortly.\n\n"
            "Reply JOIN to subscribe. Reply HELP for options."
        )

    return str(resp)


# Create/update table on startup
init_db()

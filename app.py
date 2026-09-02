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

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM_NUMBER = os.environ["TWILIO_FROM_NUMBER"]
ADMIN_PHONE = os.environ["ADMIN_PHONE"]

twilio_client = Client(
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN
)


# ----------------------------
# DATABASE HELPERS
# ----------------------------
def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:

            # Create subscribers table if it doesn't exist
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    phone_e164 TEXT PRIMARY KEY,
                    opted_in BOOLEAN NOT NULL DEFAULT TRUE,
                    source TEXT,
                    updated_at TIMESTAMPTZ NOT NULL
                )
            """)

            # Add email column without deleting existing subscribers
            cur.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS email TEXT
            """)

            # Tracks whether we are waiting for this person's email
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

                ON CONFLICT (phone_e164)
                DO UPDATE SET
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


def set_awaiting_email(
    phone_e164: str,
    awaiting: bool
):
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


def save_email(
    phone_e164: str,
    email: str
):
    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                UPDATE subscribers
                SET email = %s,
                    awaiting_email = FALSE,
                    updated_at = %s
                WHERE phone_e164 = %s
            """, (
                email.lower(),
                datetime.utcnow(),
                phone_e164
            ))

        conn.commit()


# ----------------------------
# EMAIL VALIDATION
# ----------------------------
def validate_email(value: str):
    """
    Returns:
        (True, None) if email looks valid
        (False, suggestion) if a common typo is detected
        (False, None) if format is invalid
    """

    value = value.strip().lower()

    # Basic email format validation
    if not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}",
        value
    ):
        return False, None

    username, domain = value.rsplit("@", 1)

    # Common email-provider typos
    typo_domains = {
        # Yahoo
        "yahoo.cm": "yahoo.com",
        "yahoo.con": "yahoo.com",
        "yahoo.cmo": "yahoo.com",
        "yaho.com": "yahoo.com",

        # Gmail
        "gmail.cm": "gmail.com",
        "gmail.con": "gmail.com",
        "gmail.cmo": "gmail.com",
        "gamil.com": "gmail.com",
        "gmial.com": "gmail.com",

        # Hotmail
        "hotmail.cm": "hotmail.com",
        "hotmail.con": "hotmail.com",
        "hotmail.cmo": "hotmail.com",

        # Outlook
        "outlook.cm": "outlook.com",
        "outlook.con": "outlook.com",
        "outlook.cmo": "outlook.com",

        # iCloud
        "icloud.cm": "icloud.com",
        "icloud.con": "icloud.com",
    }

    if domain in typo_domains:
        suggested_email = (
            f"{username}@{typo_domains[domain]}"
        )

        return False, suggested_email

    return True, None


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

        # Subscribe immediately
        upsert_subscriber(
            from_number,
            True
        )

        # Wait for optional email
        set_awaiting_email(
            from_number,
            True
        )

        resp.message(
            "✅ You’re subscribed.\n\n"
            "Please provide your email address, "
            "or reply SKIP if you prefer SMS reminders only.\n\n"
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

        upsert_subscriber(
            from_number,
            False
        )

        set_awaiting_email(
            from_number,
            False
        )

        resp.message(
            "You’re unsubscribed. "
            "Reply START to re-subscribe."
        )


    # ------------------------
    # START
    # ------------------------
    elif body_lower == "start":

        upsert_subscriber(
            from_number,
            True
        )

        set_awaiting_email(
            from_number,
            False
        )

        resp.message(
            "Welcome back! You’re subscribed again. "
            "Reply STOP to opt out."
        )


    # ------------------------
    # HELP
    # ------------------------
    elif body_lower == "help":

        resp.message(
            "Reply JOIN to subscribe. "
            "Reply STOP to opt out."
        )


    # ------------------------
    # SKIP EMAIL
    # ------------------------
    elif (
        body_lower == "skip"
        and is_awaiting_email(from_number)
    ):

        set_awaiting_email(
            from_number,
            False
        )

        resp.message(
            "No problem. You’ll remain subscribed "
            "to SMS reminders."
        )


    # ------------------------
    # EMAIL RESPONSE
    # ------------------------
    elif is_awaiting_email(from_number):

        valid, suggestion = validate_email(body)

        # Valid email
        if valid:

            save_email(
                from_number,
                body
            )

            resp.message(
                "Thank you! Your email address "
                "has been saved."
            )

        # Likely typo
        elif suggestion:

            resp.message(
                f"Did you mean {suggestion}?\n\n"
                "Please send your email address again, "
                "or reply SKIP."
            )

        # Invalid format
        else:

            resp.message(
                "That doesn’t appear to be a valid "
                "email address.\n\n"
                "Please check it and send it again, "
                "or reply SKIP."
            )


    # ------------------------
    # OTHER MESSAGES
    # ------------------------
    else:

        # Alert admin with incoming message
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

            # Don't break webhook if alert fails
            print(
                f"[ERROR] Failed to alert admin: {exc}"
            )


        # Reply to sender
        resp.message(
            "Your message has been received. "
            "We will get back to you shortly.\n\n"
            "Reply JOIN to subscribe. "
            "Reply HELP for options."
        )


    return str(resp)


# ----------------------------
# INITIALIZE DATABASE
# ----------------------------
init_db()

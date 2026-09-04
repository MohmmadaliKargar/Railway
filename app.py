import os
import re
from datetime import datetime, timedelta, timezone

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
# SETTINGS
# ----------------------------
EMAIL_WAIT_HOURS = 24


# ----------------------------
# DATABASE HELPERS
# ----------------------------
def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:

            # Create table if it does not already exist
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    phone_e164 TEXT PRIMARY KEY,
                    opted_in BOOLEAN NOT NULL DEFAULT TRUE,
                    source TEXT,
                    updated_at TIMESTAMPTZ NOT NULL
                )
            """)

            # Add email column without removing existing subscribers
            cur.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS email TEXT
            """)

            # Track whether we're currently waiting for an email
            cur.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS awaiting_email BOOLEAN
                NOT NULL DEFAULT FALSE
            """)

            # Track when the email request began
            cur.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS awaiting_email_since TIMESTAMPTZ
            """)

        conn.commit()


def upsert_subscriber(
    phone_e164: str,
    opted_in: bool,
    source: str = "sms_keyword"
):
    now = datetime.now(timezone.utc)

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
                now
            ))

        conn.commit()


def set_awaiting_email(
    phone_e164: str,
    awaiting: bool
):
    now = datetime.now(timezone.utc)

    with get_conn() as conn:
        with conn.cursor() as cur:

            if awaiting:

                cur.execute("""
                    UPDATE subscribers
                    SET awaiting_email = TRUE,
                        awaiting_email_since = %s,
                        updated_at = %s
                    WHERE phone_e164 = %s
                """, (
                    now,
                    now,
                    phone_e164
                ))

            else:

                cur.execute("""
                    UPDATE subscribers
                    SET awaiting_email = FALSE,
                        awaiting_email_since = NULL,
                        updated_at = %s
                    WHERE phone_e164 = %s
                """, (
                    now,
                    phone_e164
                ))

        conn.commit()


def is_awaiting_email(phone_e164: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    awaiting_email,
                    awaiting_email_since
                FROM subscribers
                WHERE phone_e164 = %s
            """, (
                phone_e164,
            ))

            row = cur.fetchone()

    if not row:
        return False

    awaiting_email, awaiting_since = row

    if not awaiting_email or not awaiting_since:
        return False

    expires_at = (
        awaiting_since
        + timedelta(hours=EMAIL_WAIT_HOURS)
    )

    # If more than 24 hours have passed,
    # clear the awaiting-email state
    if datetime.now(timezone.utc) > expires_at:

        set_awaiting_email(
            phone_e164,
            False
        )

        return False

    return True


def save_email(
    phone_e164: str,
    email: str
):
    now = datetime.now(timezone.utc)

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                UPDATE subscribers
                SET email = %s,
                    awaiting_email = FALSE,
                    awaiting_email_since = NULL,
                    updated_at = %s
                WHERE phone_e164 = %s
            """, (
                email.lower(),
                now,
                phone_e164
            ))

        conn.commit()


# ----------------------------
# EMAIL VALIDATION
# ----------------------------
def validate_email(value: str):
    """
    Returns:

    (True, None)
        Valid-looking email

    (False, suggestion)
        Common typo detected

    (False, None)
        Invalid email format
    """

    value = value.strip().lower()

    # Basic email-format validation
    if not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}",
        value
    ):
        return False, None

    username, domain = value.rsplit("@", 1)

    # Common email provider typos
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
        "icloud.cmo": "icloud.com",
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

        # Subscribe them immediately
        upsert_subscriber(
            from_number,
            True
        )

        # Start the 24-hour email collection window
        set_awaiting_email(
            from_number,
            True
        )

        resp.message(
        "✅ You’re all set!\n\n"
        "Want to never miss a Misbah reminder? 📩 "
        "Reply with your email to join our FREE email list.\n\n"
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

        # Common typo detected
        elif suggestion:

            resp.message(
                f"Did you mean {suggestion}?\n\n"
                "Please send your email address again."
            )

        # Invalid format
        else:

            resp.message(
                "That doesn’t appear to be a valid "
                "email address.\n\n"
                "Please check it and send it again."
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

            # Don't break the webhook if admin alert fails
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

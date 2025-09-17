import uuid
from datetime import datetime, date
from typing import List, Optional
import streamlit as st
# Must be first Streamlit command
st.set_page_config(page_title="Hotel Request Intake", page_icon="🏨", layout="centered")

from pydantic import BaseModel, EmailStr, validator
from sqlalchemy import (
    create_engine, text
)
import json
import requests

# ---------------------------
# Config / Secrets
# ---------------------------
# Load configuration from .streamlit/secrets.toml
DB_URI = st.secrets["db"]["uri"]
WEBHOOK_URL = st.secrets.get("WEBHOOK_URL", "")

engine = create_engine(DB_URI, pool_pre_ping=True, future=True)

# ---------------------------
# One-time table creation
# ---------------------------
DDL = """
CREATE SCHEMA IF NOT EXISTS hotel_scans;

CREATE TABLE IF NOT EXISTS hotel_scans.hotel_requests (
    id UUID PRIMARY KEY,
    created_ts TIMESTAMPTZ NOT NULL,
    destination_text TEXT NOT NULL,
    requester_email TEXT NOT NULL,
    nickname TEXT,
    check_in_date DATE NOT NULL,
    check_out_date DATE NOT NULL,
    hotel_brands_json JSONB NOT NULL,
    source TEXT NOT NULL DEFAULT 'streamlit',
    submission_ip TEXT,
    ua_hash TEXT,
    processed BOOLEAN DEFAULT FALSE,
    workbook_url TEXT,
    workbook_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_hotel_requests_check_in ON hotel_scans.hotel_requests (check_in_date);
CREATE INDEX IF NOT EXISTS idx_hotel_requests_destination ON hotel_scans.hotel_requests (destination_text);
CREATE INDEX IF NOT EXISTS idx_hotel_requests_email ON hotel_scans.hotel_requests (requester_email);
"""

try:
    with engine.begin() as conn:
        for stmt in [s.strip() for s in DDL.split(";") if s.strip()]:
            conn.execute(text(stmt))
except Exception as e:
    st.sidebar.error(f"Database error: {str(e)[:100]}")

# ---------------------------
# Validation model
# ---------------------------
CANONICAL_BRANDS = [
    "IHG (Inter Continental - Crowne Plaza - Holiday Inn - etc)",
    "Hilton (NoMad - DoubleTree - Embassy Suites - etc)",
    "Marriott (Ritz-Carlton - St. Regis - Westin - etc)",
    "Hyatt (Regency - The Standard - Grand Hyatt - etc)",
]

class Submission(BaseModel):
    destination: str
    email: EmailStr
    check_in: date
    check_out: date
    hotel_brands: List[str]

    @validator("destination", allow_reuse=True)
    def dest_nonempty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Destination is required.")
        return v.title()

    @validator("check_out", allow_reuse=True)
    def dates_valid(cls, v, values):
        check_in = values.get("check_in")
        if check_in and v <= check_in:
            raise ValueError("Check-out must be after check-in.")
        return v

    @validator("hotel_brands", allow_reuse=True)
    def brands_valid(cls, v):
        if not v:
            raise ValueError("Select at least one hotel brand.")
        invalid = [b for b in v if b not in CANONICAL_BRANDS]
        if invalid:
            raise ValueError(f"Unknown brand(s): {invalid}")
        return v

# ---------------------------
# UI
# ---------------------------
st.title("🏨 Hotel Request Intake")

with st.form("request_form", clear_on_submit=True):  # Changed to clear_on_submit=True
    st.subheader("Trip Details")
    destination = st.text_input("Destination (City, Council, or Country)")
    email = st.text_input("Your email")

    cols = st.columns(2)
    with cols[0]:
        check_in = st.date_input("Check-in Date", value=None)
    with cols[1]:
        check_out = st.date_input("Check-out Date", value=None)

    st.write("### Hotel Brand(s) 👇 (Select one or more)")
    st.caption("Click to select multiple options")
    hotel_brands = st.multiselect("", options=CANONICAL_BRANDS, help="You can select multiple hotel brands")

    submitted = st.form_submit_button("Submit")

if submitted:
    # Server-side timestamp (UTC)
    created_ts = datetime.utcnow()

    # Validate + normalize
    try:
        data = Submission(
            destination=destination,
            email=email,
            check_in=check_in,
            check_out=check_out,
            hotel_brands=hotel_brands,
        )
    except Exception as e:
        st.sidebar.error(f"Validation error: {e}")
        st.stop()

    # Build record
    request_id = str(uuid.uuid4())
    hotel_brands_json = json.dumps(data.hotel_brands)

    # Insert into DB (transaction)
    INSERT_SQL = text("""
        INSERT INTO hotel_scans.hotel_requests (
            id, created_ts, destination_text, requester_email, nickname,
            check_in_date, check_out_date, hotel_brands_json, source, submission_ip, ua_hash
        ) VALUES (
            :id, :created_ts, :destination_text, :requester_email, NULL,
            :check_in_date, :check_out_date, CAST(:hotel_brands_json AS JSONB), :source, NULL, NULL
        )
    """)

    try:
        with engine.begin() as conn:
            conn.execute(
                INSERT_SQL,
                {
                    "id": request_id,
                    "created_ts": created_ts,
                    "destination_text": data.destination,
                    "requester_email": str(data.email),
                    "check_in_date": data.check_in,
                    "check_out_date": data.check_out,
                    "hotel_brands_json": hotel_brands_json,
                    "source": "streamlit",
                },
            )
    except Exception as e:
        st.sidebar.error(f"Database error: {str(e)}")
        st.stop()

    # Trigger event (optional)
    event_ok = True
    event_msg = "No webhook configured."
    if WEBHOOK_URL:
        try:
            resp = requests.post(
                WEBHOOK_URL,
                timeout=10,
                json={
                    "request_id": request_id,
                    "created_ts": created_ts.isoformat(),
                    "destination": data.destination,
                    "email": str(data.email),
                    "check_in": str(data.check_in),
                    "check_out": str(data.check_out),
                    "hotel_brands": data.hotel_brands,
                    "source": "streamlit",
                },
            )
            if resp.status_code >= 300:
                event_ok = False
                event_msg = f"Webhook responded {resp.status_code}: {resp.text[:200]}"
            else:
                event_msg = "Webhook triggered."
        except Exception as e:
            event_ok = False
            event_msg = f"Webhook error: {e}"

    # Acknowledge
    st.sidebar.success(f"Saved! Your request ID is `{request_id}`.")
    st.sidebar.info("Your hotel request has been submitted. Our automated system will search for the best rates and send you an email within the next hour.")
    if event_ok and event_msg != "No webhook configured.":
        st.sidebar.info(event_msg)
    elif not event_ok:
        st.sidebar.warning(f"Event trigger failed: {event_msg}")

    # with st.sidebar.expander("Submitted payload (sanitized)"):
    #     st.json({
    #         "id": request_id,
    #         "created_ts": created_ts.isoformat(),
    #         "destination": data.destination,
    #         "email": str(data.email),
    #         "check_in": str(data.check_in),
    #         "check_out": str(data.check_out),
    #         "hotel_brands": data.hotel_brands,
    #     })

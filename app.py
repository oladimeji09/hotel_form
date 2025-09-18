
import uuid
from datetime import datetime, date
from typing import List, Optional
import streamlit as st
import json
import requests
import os
from pydantic import BaseModel, EmailStr, validator
from supabase import create_client, Client

# Must be first Streamlit command
st.set_page_config(page_title="Hotel Request Intake", page_icon="🏨", layout="centered")


# ---------------------------
# Config / Secrets
# ---------------------------
# Load configuration from environment variables
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# (Supabase manages the table schema. No need for local DDL or engine setup.)

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

    # Prepare data for Supabase
    supabase_payload = {
        "id": request_id,
        "created_ts": created_ts.isoformat(),
        "destination_text": data.destination,
        "requester_email": str(data.email),
        "nickname": None,
        "check_in_date": str(data.check_in),
        "check_out_date": str(data.check_out),
        "hotel_brands_json": hotel_brands_json,
        "source": "streamlit",
        "submission_ip": None,
        "ua_hash": None,
        "processed": False,
        "workbook_url": None,
        "workbook_id": None,
    }

    try:
        response = supabase.table("hotel_requests").insert(supabase_payload).execute()
        if hasattr(response, "status_code") and response.status_code >= 300:
            st.sidebar.error(f"Supabase error: {getattr(response, 'data', response)}")
            st.stop()
    except Exception as e:
        st.sidebar.error(f"Supabase error: {e}")
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


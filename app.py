from urllib.parse import urlencode
# Helper to get request_id from URL query params
def get_request_id_from_url():
    query_params = st.query_params if hasattr(st, 'query_params') else st.experimental_get_query_params()
    req_id = query_params.get("request_id")
    if isinstance(req_id, list):
        req_id = req_id[0]
    return req_id
from dotenv import load_dotenv

import uuid
from datetime import datetime, date
from typing import List, Optional
import streamlit as st
import json
import requests
import os
from pydantic import BaseModel, EmailStr, field_validator
from supabase import create_client, Client

# Must be first Streamlit command
# Must be first Streamlit command
load_dotenv()
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


    @field_validator("destination")
    @classmethod
    def dest_nonempty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Destination is required.")
        return v.title()


    @field_validator("check_out")
    @classmethod
    def dates_valid(cls, v, values):
        check_in = values.data.get("check_in") if hasattr(values, 'data') else values.get("check_in")
        if check_in and v <= check_in:
            raise ValueError("Check-out must be after check-in.")
        return v


    @field_validator("hotel_brands")
    @classmethod
    def brands_valid(cls, v):
        if not v:
            raise ValueError("Select at least one hotel brand.")
        invalid = [b for b in v if b not in CANONICAL_BRANDS]
        if invalid:
            raise ValueError(f"Unknown brand(s): {invalid}")
        return v

req_id = get_request_id_from_url()

# If not viewing results, show the form
if not req_id:
    st.title("🏨 Hotel Request Intake")
    with st.form("request_form", clear_on_submit=True):
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

        # Store request_id in session state for later use
        st.session_state["request_id"] = request_id

        # Redirect to URL with ?request_id=... for persistent results
        base_url = st.get_url() if hasattr(st, 'get_url') else None
        if base_url:
            url = base_url.split('?')[0] + '?' + urlencode({"request_id": request_id})
            st.markdown(f"[View your results here]({url})  ")
            st.info("Bookmark or save this link to revisit your results later.")
            st.experimental_set_query_params(request_id=request_id)
        else:
            st.info(f"Your request ID is `{request_id}`. Use this to view your results later.")

# --- Visualize vw_top_results for current request_id ---
if req_id:
    st.title("🏨 Your Top Hotel Results")
    try:
        vw_response = supabase.table("vw_top_results").select("*").eq("request_id", req_id).execute()
        if hasattr(vw_response, "data") and vw_response.data:
            import pandas as pd
            from st_aggrid import AgGrid, GridOptionsBuilder, JsCode
            df = pd.DataFrame(vw_response.data)
            # Only show the specified columns, fill missing columns with empty/default values
            show_cols = [
                "rank", "name", "distance", "reviews", "rating", "price", "discount_pct", "retail_price", "booking_url", "hotel_brand", "currency"
            ]
            for col in show_cols:
                if col not in df.columns:
                    df[col] = "" if col not in ["price", "discount_pct", "retail_price", "distance", "reviews", "rank", "rating"] else 0
            df = df[show_cols]

            # Add icons for hotel_brand and booking_url
            brand_icons = {
                "Marriott": "🏨",
                "IHG": "🌐",
                "Hilton": "👑",
                "Hyatt": "✨"
            }
            
            # Brand color mapping for buttons
            brand_colors = {
                "Marriott": "#1C3F94",  # Marriott blue
                "IHG": "#D55216",       # IHG orange/red
                "Hilton": "#00263E",    # Hilton dark blue
                "Hyatt": "#7D2248"      # Hyatt burgundy
            }
            
            def brand_icon(brand):
                return brand_icons.get(brand, "🏨") + " " + str(brand)
            
            df["hotel_brand"] = df["hotel_brand"].apply(brand_icon)
            
            # Store original brand for button color
            df["brand_key"] = df["hotel_brand"].apply(lambda x: next((k for k in brand_icons.keys() if k in x), "default"))

            def booking_link(row):
                url = row["booking_url"]
                brand = row["brand_key"]
                color = brand_colors.get(brand, "#3a7bd5")  # Default blue if brand not found
                return f'<a href="{url}" target="_blank" style="background-color: {color};">🔗 Book</a>' if url else ""
            
            # Apply the function row-wise to get colored buttons based on brand
            df["booking_url"] = df.apply(booking_link, axis=1)

            # Filtering options
            brands = sorted(set(df["hotel_brand"]))
            selected_brands = st.multiselect("Filter by Brand", brands, default=brands)
            filtered_df = df[df["hotel_brand"].isin(selected_brands)].copy()
            # Ensure numeric columns for filtering
            for col in ["price", "distance", "discount_pct", "reviews"]:
                filtered_df[col] = pd.to_numeric(filtered_df[col], errors="coerce").fillna(0)
            # Price filter
            min_price, max_price = int(filtered_df["price"].min()), int(filtered_df["price"].max()) if not filtered_df.empty else (0, 0)
            price_range = st.slider("Price Range", min_price, max_price, (min_price, max_price), step=1, key="price_slider") if min_price != max_price else (min_price, max_price)
            filtered_df = filtered_df[(filtered_df["price"] >= price_range[0]) & (filtered_df["price"] <= price_range[1])]
            # Distance filter
            min_dist, max_dist = int(filtered_df["distance"].min()), int(filtered_df["distance"].max()) if not filtered_df.empty else (0, 0)
            dist_range = st.slider("Distance Range (km)", min_dist, max_dist, (min_dist, max_dist), step=1, key="dist_slider") if min_dist != max_dist else (min_dist, max_dist)
            filtered_df = filtered_df[(filtered_df["distance"] >= dist_range[0]) & (filtered_df["distance"] <= dist_range[1])]
            # Discount filter
            min_disc, max_disc = int(filtered_df["discount_pct"].min()), int(filtered_df["discount_pct"].max()) if not filtered_df.empty else (0, 0)
            disc_range = st.slider("Discount % Range", min_disc, max_disc, (min_disc, max_disc), step=1, key="disc_slider") if min_disc != max_disc else (min_disc, max_disc)
            filtered_df = filtered_df[(filtered_df["discount_pct"] >= disc_range[0]) & (filtered_df["discount_pct"] <= disc_range[1])]
            # Reviews filter
            min_rev, max_rev = int(filtered_df["reviews"].min()), int(filtered_df["reviews"].max()) if not filtered_df.empty else (0, 0)
            rev_range = st.slider("Reviews Range", min_rev, max_rev, (min_rev, max_rev), step=1, key="rev_slider") if min_rev != max_rev else (min_rev, max_rev)
            filtered_df = filtered_df[(filtered_df["reviews"] >= rev_range[0]) & (filtered_df["reviews"] <= rev_range[1])]

            # Create a professional-looking table with custom styling
            st.write("### Your Matched Hotels")
            st.write("(Click 🔗 to book)")
            # Add custom CSS for a black table background with white text
            st.markdown("""
            <style>
            .hotel-table {
                border-collapse: collapse;
                width: 100%;
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
                border-radius: 8px;
                overflow: hidden;
                margin-bottom: 24px;
                background: #111;
            }
            .hotel-table thead {
                background: #222;
                color: #fff;
                border-bottom: 2px solid #444;
            }
            .hotel-table th {
                padding: 12px 15px;
                text-align: left;
                font-weight: 600;
                text-transform: uppercase;
                font-size: 12px;
                letter-spacing: 0.5px;
                color: #fff;
                background: #222;
            }
            .hotel-table td {
                padding: 12px 15px;
                border-bottom: 1px solid #222;
                vertical-align: middle;
                color: #fff;
                background-color: #111;
            }
            .hotel-table tr:last-child td {
                border-bottom: none;
            }
            .hotel-table tr:hover td {
                background-color: #222 !important;
            }
            .hotel-table a {
                display: inline-block;
                padding: 6px 12px;
                color: white;
                text-decoration: none;
                border-radius: 4px;
                font-weight: 500;
                transition: all 0.2s ease;
            }
            .hotel-table a:hover {
                opacity: 0.9;
                box-shadow: 0 2px 5px rgba(0, 0, 0, 0.2);
            }
            /* Add specific styling for important columns */
            .hotel-table td:nth-child(2) {
                font-weight: 600; /* Hotel name */
            }
            .hotel-table td:nth-child(6) {
                font-weight: 700;
                color: #47ffb2; /* Price in green (bright for dark bg) */
            }
            .hotel-table td:nth-child(7) {
                font-weight: 600;
                color: #ff6b6b; /* Discount in red (bright for dark bg) */
            }
            </style>
            """, unsafe_allow_html=True)
            # Convert DataFrame to HTML with custom class
            html_table = filtered_df.to_html(escape=False, index=False, classes='hotel-table')
            # Display the styled table
            st.markdown(html_table, unsafe_allow_html=True)
        else:
            st.info("No results found for your request yet. Please check back soon.")
    except Exception as e:
        st.error(f"Error fetching results: {e}")

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


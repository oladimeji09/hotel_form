from urllib.parse import urlencode
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

# Must be first - load env before using streamlit
load_dotenv()

# Check if we're on the results page BEFORE setting page config
# This must happen before any other Streamlit command
query_params = st.query_params if hasattr(st, 'query_params') else st.experimental_get_query_params()
req_id_from_url = query_params.get("request_id")
if isinstance(req_id_from_url, list):
    req_id_from_url = req_id_from_url[0]

# Set page config based on whether we're showing results or form
if req_id_from_url:
    # Results page - use wide layout
    st.set_page_config(page_title="Hotel Results", page_icon="🏨", layout="wide")
else:
    # Form page - use centered layout
    st.set_page_config(page_title="Hotel Request Intake", page_icon="🏨", layout="centered")

# Helper to get request_id from URL query params
def get_request_id_from_url():
    query_params = st.query_params if hasattr(st, 'query_params') else st.experimental_get_query_params()
    req_id = query_params.get("request_id")
    if isinstance(req_id, list):
        req_id = req_id[0]
    return req_id


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
            df = pd.DataFrame(vw_response.data)
            # Only show the specified columns, fill missing columns with empty/default values
            # Reorder columns: name, hotel brand, distance, price, discount, retail price, rating, reviews, booking link, currency
            show_cols = [
                "name", "hotel_brand", "distance", "price", "discount_pct", "retail_price", "rating", "reviews", "booking_url", "currency"
            ]
            for col in show_cols:
                if col not in df.columns:
                    df[col] = "" if col not in ["price", "discount_pct", "retail_price", "distance", "reviews", "rating"] else 0
            
            # Select only the columns we want
            df = df[show_cols]
            
            # Add icons for hotel_brand and booking_url BEFORE renaming
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
            
            # Don't add emojis, just keep the brand name
            # df["hotel_brand"] is already the brand name, no transformation needed
            
            # Store original brand for button color (before renaming)
            df["brand_key"] = df["hotel_brand"].apply(lambda x: next((k for k in brand_icons.keys() if k in x), "default"))

            def booking_link(row):
                url = row["booking_url"]
                brand = row["brand_key"]
                color = brand_colors.get(brand, "#3a7bd5")  # Default blue if brand not found
                return f'<a href="{url}" target="_blank" style="background-color: {color};">🔗 Book</a>' if url else ""
            
            # Apply the function row-wise to get colored buttons based on brand (before renaming)
            df["booking_url"] = df.apply(booking_link, axis=1)
            
            # Convert rating to stars (without numeric display)
            def rating_to_stars(rating):
                try:
                    rating_float = float(rating)
                    full_stars = int(rating_float)
                    half_star = 1 if (rating_float - full_stars) >= 0.5 else 0
                    empty_stars = 5 - full_stars - half_star
                    stars = "⭐" * full_stars + ("✨" if half_star else "") + "☆" * empty_stars
                    return stars
                except:
                    return "☆☆☆☆☆"
            
            df["rating"] = df["rating"].apply(rating_to_stars)
            # Store numeric rating separately for filtering/sorting
            df["rating_value"] = pd.to_numeric(df["rating"].apply(lambda x: float(str(x).replace('⭐','1').replace('✨','0.5').replace('☆','0')[:3]) if isinstance(x, str) else 0), errors="coerce").fillna(0)
            
            # Store numeric versions BEFORE renaming columns
            df["price_numeric"] = pd.to_numeric(df["price"], errors="coerce").fillna(0)
            # Parse distance: extract numeric value from strings like "0.5 miles" or "1.2 km"
            df["distance_numeric"] = df["distance"].apply(lambda x: float(str(x).split()[0]) if isinstance(x, str) and str(x).split()[0].replace('.','',1).isdigit() else 0)
            df["discount_numeric"] = pd.to_numeric(df["discount_pct"], errors="coerce").fillna(0)
            df["reviews_numeric"] = pd.to_numeric(df["reviews"], errors="coerce").fillna(0)
            df["retail_price_numeric"] = pd.to_numeric(df["retail_price"], errors="coerce").fillna(0)
            
            # NOW rename columns for display (do this LAST)
            df = df.rename(columns={
                "hotel_brand": "hotel brand",
                "discount_pct": "discount",
                "retail_price": "retail price",
                "booking_url": "booking link"
            })

            # Filtering options (moved to top)
            st.write("### 🔍 Filter Options")
            brands = sorted(set(df["hotel brand"]))
            selected_brands = st.multiselect("Filter by Brand", brands, default=brands)
            filtered_df = df[df["hotel brand"].isin(selected_brands)].copy()
            
            # Price filter
            min_price, max_price = int(filtered_df["price_numeric"].min()), int(filtered_df["price_numeric"].max()) if not filtered_df.empty else (0, 0)
            price_range = st.slider("Price Range", min_price, max_price, (min_price, max_price), step=1, key="price_slider") if min_price != max_price else (min_price, max_price)
            filtered_df = filtered_df[(filtered_df["price_numeric"] >= price_range[0]) & (filtered_df["price_numeric"] <= price_range[1])]
            # Distance filter
            min_dist, max_dist = int(filtered_df["distance_numeric"].min()), int(filtered_df["distance_numeric"].max()) if not filtered_df.empty else (0, 0)
            dist_range = st.slider("Distance Range (km)", min_dist, max_dist, (min_dist, max_dist), step=1, key="dist_slider") if min_dist != max_dist else (min_dist, max_dist)
            filtered_df = filtered_df[(filtered_df["distance_numeric"] >= dist_range[0]) & (filtered_df["distance_numeric"] <= dist_range[1])]
            # Discount filter
            min_disc, max_disc = int(filtered_df["discount_numeric"].min()), int(filtered_df["discount_numeric"].max()) if not filtered_df.empty else (0, 0)
            disc_range = st.slider("Discount % Range", min_disc, max_disc, (min_disc, max_disc), step=1, key="disc_slider") if min_disc != max_disc else (min_disc, max_disc)
            filtered_df = filtered_df[(filtered_df["discount_numeric"] >= disc_range[0]) & (filtered_df["discount_numeric"] <= disc_range[1])]
            # Rating filter
            if "rating_value" in filtered_df.columns:
                filtered_df["rating_numeric"] = filtered_df["rating_value"]
            else:
                filtered_df["rating_numeric"] = 0.0
            min_rating, max_rating = float(filtered_df["rating_numeric"].min()), float(filtered_df["rating_numeric"].max()) if not filtered_df.empty else (0.0, 5.0)
            rating_range = st.slider("Rating Range", min_rating, max_rating, (min_rating, max_rating), step=0.1, key="rating_slider") if min_rating != max_rating else (min_rating, max_rating)
            filtered_df = filtered_df[(filtered_df["rating_numeric"] >= rating_range[0]) & (filtered_df["rating_numeric"] <= rating_range[1])]
            
            st.divider()  # Visual separator between filters and sort

            # Initialize session state for sorting
            if 'sort_by' not in st.session_state:
                st.session_state.sort_by = 'price'
            if 'sort_asc' not in st.session_state:
                st.session_state.sort_asc = True
            
            # Sorting controls with clickable column headers (only sortable columns)
            st.write("### 📊 Sort Options")
            
            # Create clickable column header buttons for sortable columns only
            sort_cols = st.columns(4)
            col_names = ["distance", "price", "discount", "rating"]
            col_labels = ["📍 Distance", "💰 Price", "💸 Discount", "⭐ Rating"]
            
            for idx, (col_name, col_label) in enumerate(zip(col_names, col_labels)):
                with sort_cols[idx]:
                    # Show arrow indicator if this column is currently sorted
                    if st.session_state.sort_by == col_name:
                        arrow = " ⬆️" if st.session_state.sort_asc else " ⬇️"
                        label = f"**{col_label}{arrow}**"
                    else:
                        label = col_label
                    
                    if st.button(label, key=f"sort_{col_name}", use_container_width=True):
                        # Toggle sort direction if same column, otherwise set to ascending
                        if st.session_state.sort_by == col_name:
                            st.session_state.sort_asc = not st.session_state.sort_asc
                        else:
                            st.session_state.sort_by = col_name
                            st.session_state.sort_asc = True
                        st.rerun()  # Force immediate rerun to update sorting
            
            # Apply sorting based on session state
            # Map display columns to numeric columns for sorting
            sort_col_map = {
                "distance": "distance_numeric",
                "rating": "rating_numeric",
                "reviews": "reviews_numeric",
                "price": "price_numeric",
                "discount": "discount_numeric",
                "retail price": "retail_price_numeric"
            }
            actual_sort_col = sort_col_map.get(st.session_state.sort_by, st.session_state.sort_by)
            filtered_df = filtered_df.sort_values(by=actual_sort_col, ascending=st.session_state.sort_asc)

            # Create a professional-looking table with custom styling
            st.write("### Your Matched Hotels")
            st.write(f"Sorted by: **{st.session_state.sort_by}** {'⬆️ Ascending' if st.session_state.sort_asc else '⬇️ Descending'}")
            # Drop the temporary columns before display
            cols_to_drop = ["brand_key", "price_numeric", "distance_numeric", "discount_numeric", "reviews_numeric", "retail_price_numeric", "rating_numeric"]
            # Also drop rating_value if it exists
            if "rating_value" in filtered_df.columns:
                cols_to_drop.append("rating_value")
            display_df = filtered_df.drop(columns=cols_to_drop)
            
            # Add custom CSS for a black table background with white text, frozen header and first column
            st.markdown("""
            <style>
            .table-wrapper {
                max-height: 600px;
                overflow: auto;
                position: relative;
                border: 1px solid #444;
                border-radius: 8px;
                -webkit-overflow-scrolling: touch; /* Smooth scrolling on iOS */
            }
            .hotel-table {
                border-collapse: collapse;
                width: 100%;
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: #111;
                margin: 0;
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
                position: sticky;
                top: 0;
                z-index: 10;
            }
            /* Freeze first column (name) */
            .hotel-table th:first-child,
            .hotel-table td:first-child {
                position: sticky;
                left: 0;
                z-index: 5;
                background: #111;
            }
            .hotel-table th:first-child {
                z-index: 15;
                background: #222;
            }
            .hotel-table td {
                padding: 12px 15px;
                border-bottom: 1px solid #222;
                vertical-align: middle;
                color: #fff;
                background-color: #111;
                white-space: nowrap;
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
            .hotel-table td:first-child {
                font-weight: 600; /* Hotel name - first column */
                min-width: 40px;
            }
            .hotel-table td:nth-child(4) {
                font-weight: 700;
                color: #47ffb2; /* Price in green (bright for dark bg) */
            }
            .hotel-table td:nth-child(5) {
                font-weight: 600;
                color: #ff6b6b; /* Discount in red (bright for dark bg) */
            }
            
            /* Mobile-responsive styles */
            @media screen and (max-width: 768px) {
                .table-wrapper {
                    max-height: 500px;
                    border-radius: 4px;
                }
                .hotel-table th,
                .hotel-table td {
                    padding: 8px 10px;
                    font-size: 11px;
                }
                .hotel-table th:first-child,
                .hotel-table td:first-child {
                    min-width: 20px; /* Even smaller hotel name column on mobile */
                }
                .hotel-table a {
                    padding: 5px 10px;
                    font-size: 11px;
                }
                /* Make sure frozen columns work on mobile */
                .hotel-table th:first-child,
                .hotel-table td:first-child {
                    box-shadow: 2px 0 5px rgba(0, 0, 0, 0.3); /* Add shadow for depth */
                }
            }
            
            /* Very small screens - stack info differently */
            @media screen and (max-width: 480px) {
                .hotel-table th,
                .hotel-table td {
                    padding: 6px 8px;
                    font-size: 10px;
                }
                .hotel-table th:first-child,
                .hotel-table td:first-child {
                    min-width: 40px;
                }
            }
            </style>
            """, unsafe_allow_html=True)
            # Convert DataFrame to HTML with custom class and wrap in scrollable div
            # Use escape=False to properly render HTML links and emojis
            html_table = display_df.to_html(escape=False, index=False, classes='hotel-table')
            # Display the styled table wrapped in a scrollable container
            st.markdown(f'<div class="table-wrapper">{html_table}</div>', unsafe_allow_html=True)
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


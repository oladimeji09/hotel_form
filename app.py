from urllib.parse import urlencode
from dotenv import load_dotenv
import time
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
try:
    # Streamlit 1.30+
    query_params = st.query_params
    req_id_from_url = query_params.get("request_id", None)
except AttributeError:
    # Older Streamlit versions
    query_params = st.experimental_get_query_params()
    req_id_from_url = query_params.get("request_id", [None])[0]

# Set page config based on whether we're showing results or form
if req_id_from_url:
    # Results page - use wide layout
    st.set_page_config(page_title="Hotel Results", page_icon="🏨", layout="wide")
else:
    # Form page - use centered layout
    st.set_page_config(page_title="Hotel Request Intake", page_icon="🏨", layout="centered")

# Helper to get request_id from URL query params
def get_request_id_from_url():
    try:
        # Streamlit 1.30+
        return st.query_params.get("request_id", None)
    except AttributeError:
        # Older Streamlit versions
        params = st.experimental_get_query_params()
        req_id = params.get("request_id", [None])
        return req_id[0] if isinstance(req_id, list) else req_id


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
            raise ValueError("Please enter a destination.")
        return v.title()


    @field_validator("check_out")
    @classmethod
    def dates_valid(cls, v, values):
        check_in = values.data.get("check_in") if hasattr(values, 'data') else values.get("check_in")
        if check_in and v <= check_in:
            raise ValueError("Please select a check-out date that is after your check-in date.")
        return v


    @field_validator("hotel_brands")
    @classmethod
    def brands_valid(cls, v):
        if not v:
            raise ValueError("Please select at least one hotel brand.")
        invalid = [b for b in v if b not in CANONICAL_BRANDS]
        if invalid:
            raise ValueError(f"Please select valid hotel brands only.")
        return v

req_id = get_request_id_from_url()

# Debug: Show current state (remove in production)
# st.sidebar.info(f"Debug: req_id = {req_id}")

# If not viewing results, show the form
if not req_id:
    st.title("🏨 Hotel Request Intake")
    with st.form("request_form", clear_on_submit=False):
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
        validation_error = None
        try:
            data = Submission(
                destination=destination,
                email=email,
                check_in=check_in,
                check_out=check_out,
                hotel_brands=hotel_brands,
            )
        except Exception as e:
            # Extract the plain English message from the Pydantic error
            error_str = str(e)
            
            # Try to extract just the "Value error, MESSAGE" part
            if "Value error," in error_str:
                # Split on "Value error," and take the message before the metadata
                parts = error_str.split("Value error,", 1)[1]
                # Take only the message before [type=... appears
                validation_error = parts.split("[type=")[0].strip()
            elif "Field required" in error_str:
                validation_error = "Please fill in all required fields."
            elif "value is not a valid email address" in error_str.lower():
                validation_error = "Please enter a valid email address."
            else:
                # Fallback - try to get first sentence
                validation_error = error_str.split(".")[0].strip()
            
            st.toast(f"⚠️ {validation_error}", icon="⚠️")

        if not validation_error:
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

            submission_error = None
            try:
                response = supabase.table("hotel_requests").insert(supabase_payload).execute()
                if hasattr(response, "status_code") and response.status_code >= 300:
                    submission_error = "Unable to submit your request. Please try again."
                    st.toast(f"⚠️ {submission_error}", icon="⚠️")
            except Exception as e:
                submission_error = "Unable to submit your request. Please try again."
                st.toast(f"⚠️ {submission_error}", icon="⚠️")

            if not submission_error:
                # Store request_id in session state for later use
                st.session_state["request_id"] = request_id
                
                # Show success message and redirect
                st.success("✅ Request submitted! Redirecting to progress tracker...")
                
                # Redirect to URL with ?request_id=... for persistent results and progress tracking
                try:
                    # Streamlit 1.30+ uses st.query_params as a dict-like object
                    st.query_params["request_id"] = request_id
                except (AttributeError, TypeError):
                    # Older Streamlit versions
                    st.experimental_set_query_params(request_id=request_id)
                
                time.sleep(1)  # Brief pause so user sees the success message
                st.rerun()  # Auto-redirect to results page with progress bar

# --- Visualize vw_top_results for current request_id ---
if req_id:
    # Fetch request details from hotel_requests table
    try:
        request_details_resp = supabase.table("hotel_requests").select("destination_text, check_in_date, check_out_date").eq("id", req_id).limit(1).execute()
        request_details = None
        if hasattr(request_details_resp, "data") and request_details_resp.data:
            request_details = request_details_resp.data[0]
    except:
        request_details = None
    
    # Helper function to format date as "1st Nov 24"
    def format_date(date_str):
        try:
            from datetime import datetime
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            day = date_obj.day
            # Add ordinal suffix (st, nd, rd, th)
            if 10 <= day % 100 <= 20:
                suffix = 'th'
            else:
                suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
            return date_obj.strftime(f"%d{suffix} %b %y").lstrip('0')
        except:
            return date_str
    
    # Add "New Search" button at top right
    col1, col2 = st.columns([3, 1])
    with col1:
        st.title("🏨 Your Hotel Results")
        # Display request details if available
        if request_details:
            destination = request_details.get("destination_text", "N/A")
            check_in_raw = request_details.get("check_in_date", "N/A")
            check_out_raw = request_details.get("check_out_date", "N/A")
            check_in = format_date(check_in_raw)
            check_out = format_date(check_out_raw)
            st.markdown(f"<p style='font-size: 18px; margin-top: -10px;'>📍 <strong>{destination}</strong> • 📅 {check_in} → {check_out}</p>", unsafe_allow_html=True)
    with col2:
        st.write("")  # Add spacing to align button with title
        if st.button("🔍 New Search", type="secondary", use_container_width=True):
            # Clear query params and redirect to form
            try:
                st.query_params.clear()
            except (AttributeError, TypeError):
                st.experimental_set_query_params()
            st.rerun()
    
    # Add CSS for mobile responsiveness - 2x2 grid on mobile
    st.markdown("""
    <style>
    /* Make filters and sort options 2x2 grid on mobile - Samsung Galaxy S10 viewport is 360x760 */
    @media screen and (max-width: 768px), screen and (max-device-width: 768px) {
        /* Target all Streamlit horizontal blocks */
        .row-widget.stHorizontal,
        [data-testid="stHorizontalBlock"],
        .element-container [data-testid="column"] {
            display: flex !important;
            flex-wrap: wrap !important;
            gap: 10px !important;
        }
        
        /* Make each column take roughly half width */
        [data-testid="column"] {
            width: calc(50% - 5px) !important;
            flex: 0 0 calc(50% - 5px) !important;
            min-width: calc(50% - 5px) !important;
            max-width: calc(50% - 5px) !important;
            box-sizing: border-box !important;
        }
        
        /* Force gap columns to be hidden */
        div[data-testid="column"]:has(> div:empty),
        div[data-testid="column"] > div:empty {
            display: none !important;
            width: 0 !important;
            flex: 0 0 0 !important;
        }
        
        /* Make subheaders larger on mobile */
        .stMarkdown h3 {
            font-size: 1.3rem !important;
        }
        
        /* Adjust form elements for mobile */
        .stMultiSelect, .stSlider {
            font-size: 0.85rem !important;
        }
        
        /* Ensure buttons stack nicely */
        button[kind="secondary"], button[kind="primary"] {
            font-size: 0.9rem !important;
            padding: 0.4rem 0.6rem !important;
        }
    }
    </style>
    """, unsafe_allow_html=True)
    
    # Add JavaScript to request notification permission and trigger notification when ready
    st.markdown("""
    <script>
    // Request notification permission on page load
    if ('Notification' in window && Notification.permission === 'default') {
        Notification.requestPermission();
    }
    
    // Function to show browser notification
    function showNotification() {
        if ('Notification' in window && Notification.permission === 'granted') {
            new Notification('🏨 Hotel Results Ready!', {
                body: 'Your hotel search results are now available to view.',
                icon: '🏨',
                badge: '🏨'
            });
        }
    }
    
    // Make function globally available
    window.showHotelNotification = showNotification;
    </script>
    """, unsafe_allow_html=True)

    # Wait for processing to complete (polls up to 10 minutes)
    def wait_for_results(req_id, timeout_seconds=600, poll_interval=10):
        """Poll vw_top_results.workbook_url for req_id for up to timeout_seconds.
           Shows a progress bar and countdown. Returns True if results ready, False on timeout/error.
        """
        try:
            progress = st.progress(0)
            status_placeholder = st.empty()
            start = time.time()
            end_time = start + timeout_seconds
            while time.time() <= end_time:
                # Check if results are available by querying hotel_requests for workbook_url
                resp = supabase.table("hotel_requests").select("workbook_url").eq("id", req_id).limit(1).execute()
                results_ready = False
                if hasattr(resp, "data") and resp.data:
                    row = resp.data[0]
                    workbook_url = row.get("workbook_url")
                    results_ready = bool(workbook_url)  # Results ready if workbook_url exists
                
                    elapsed = int(time.time() - start)
                    pct = min(1.0, elapsed / float(timeout_seconds))
                    progress.progress(pct)
                    
                    rem = max(0, int(end_time - time.time()))
                    mins = rem // 60
                    secs = rem % 60
                    status_placeholder.markdown(f"🔍 Processing your search... Typically takes 5-10 minutes. Time remaining: **{mins:02d}:{secs:02d}**")
                
                if results_ready:
                    progress.progress(1.0)
                    status_placeholder.empty()
                    st.toast("Your results are ready!", icon="✅")
                    
                    # Trigger browser notification
                    st.markdown("""
                    <script>
                    if (window.showHotelNotification) {
                        window.showHotelNotification();
                    }
                    </script>
                    """, unsafe_allow_html=True)
                    
                    time.sleep(1)  # Brief pause so user sees the toast and notification
                    return True
                
                time.sleep(poll_interval)
            
            # timed out
            progress.empty()
            status_placeholder.markdown("⏳ Your search is still processing. Please check your email in 5 minutes for the results link.")
            return False
        except Exception as e:
            st.error(f"Error checking results status: {e}")
            return False
    
    # Only run wait_for_results once per request_id using session state
    session_key = f"results_checked_{req_id}"
    if session_key not in st.session_state:
        wait_for_results(req_id, timeout_seconds=600, poll_interval=10)
        st.session_state[session_key] = True  # Mark as checked so it doesn't run again
    
    try:
        vw_response = supabase.table("vw_top_results").select("*").eq("request_id", req_id).execute()
        if hasattr(vw_response, "data") and vw_response.data:
            import pandas as pd
            df = pd.DataFrame(vw_response.data)
            # Only show the specified columns, fill missing columns with empty/default values
            # Reorder columns: name, hotel brand, distance, price, discount, retail price, rating, reviews, booking link, currency
            # Map currency code to symbol
            currency_map = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "$", "AUD": "$", "JPY": "¥", "CNY": "¥", "INR": "₹"}

            # Store numeric price values BEFORE adding currency symbols
            df["price_numeric"] = pd.to_numeric(df["price"], errors="coerce").fillna(0)
            df["retail_price_numeric"] = pd.to_numeric(df["retail_price"], errors="coerce").fillna(0)
            
            # Add currency symbol to price and retail_price columns
            df["price"] = df.apply(lambda row: f"{currency_map.get(str(row['currency']), '')}{row['price']}" if pd.notna(row['price']) else "", axis=1)
            df["retail_price"] = df.apply(lambda row: f"{currency_map.get(str(row['currency']), '')}{row['retail_price']}" if pd.notna(row['retail_price']) else "", axis=1)

            # Only show the specified columns, fill missing columns with empty/default values
            # Reorder columns: name, hotel brand, distance, price, discount, retail price, rating, reviews, booking link
            show_cols = [
                "name", "hotel_brand", "distance", "price", "discount_pct", "retail_price", "rating_float", "reviews", "booking_url"
            ]
            for col in show_cols:
                if col not in df.columns:
                    df[col] = "" if col not in ["price", "discount_pct", "retail_price", "distance", "reviews", "rating_float"] else 0

            # Keep numeric columns for filtering/sorting, along with the display columns
            keep_cols = show_cols + ["price_numeric", "retail_price_numeric"]
            df = df[keep_cols]
            
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
            
            # Store numeric rating for filtering/sorting BEFORE converting to stars
            df["rating_value"] = pd.to_numeric(df["rating_float"], errors="coerce").fillna(0)
            # Convert rating_float to stars for display and rename to 'rating'
            df["rating"] = df["rating_float"].apply(rating_to_stars)
            
            # Store numeric versions BEFORE renaming columns
            # Parse distance: extract numeric value from strings like "0.5 miles" or "1.2 km"
            df["distance_numeric"] = df["distance"].apply(lambda x: float(str(x).split()[0]) if isinstance(x, str) and str(x).split()[0].replace('.','',1).isdigit() else 0)
            df["discount_numeric"] = pd.to_numeric(df["discount_pct"], errors="coerce").fillna(0)
            df["reviews_numeric"] = pd.to_numeric(df["reviews"], errors="coerce").fillna(0)
            
            # Add % sign to discount values
            df["discount_pct"] = df["discount_pct"].apply(lambda x: f"{x}%" if pd.notna(x) and x != 0 else "0%")
            
            # Drop rating_float since we now have rating column
            df = df.drop(columns=["rating_float"])
            
            # NOW rename columns for display (do this LAST)
            df = df.rename(columns={
                "hotel_brand": "hotel brand",
                "discount_pct": "discount",
                "retail_price": "retail price",
                "booking_url": "booking link"
            })

            # Filtering options in 2x2 grid layout with spacing
            st.write("### 🔍 Filter Options")
            
            # First row: Filter by Brand | Discount % Range (with gap spacing)
            row1_col1, row1_gap, row1_col2 = st.columns([1, 0.1, 1])
            with row1_col1:
                brands = sorted(set(df["hotel brand"]))
                selected_brands = st.multiselect("Filter by Brand", brands, default=brands)
            with row1_col2:
                min_disc, max_disc = int(df["discount_numeric"].min()), int(df["discount_numeric"].max()) if not df.empty else (0, 0)
                disc_range = st.slider("Discount % Range", min_disc, max_disc, (min_disc, max_disc), step=1, key="disc_slider") if min_disc != max_disc else (min_disc, max_disc)
            
            # Apply brand filter first
            filtered_df = df[df["hotel brand"].isin(selected_brands)].copy()
            
            # Second row: Distance Range (Miles) | Price Range (with gap spacing)
            row2_col1, row2_gap, row2_col2 = st.columns([1, 0.1, 1])
            with row2_col1:
                min_dist, max_dist = int(filtered_df["distance_numeric"].min()), int(filtered_df["distance_numeric"].max()) if not filtered_df.empty else (0, 0)
                dist_range = st.slider("Distance Range (Miles)", min_dist, max_dist, (min_dist, max_dist), step=1, key="dist_slider") if min_dist != max_dist else (min_dist, max_dist)
            with row2_col2:
                min_price, max_price = int(filtered_df["price_numeric"].min()), int(filtered_df["price_numeric"].max()) if not filtered_df.empty else (0, 0)
                price_range = st.slider("Price Range", min_price, max_price, (min_price, max_price), step=1, key="price_slider") if min_price != max_price else (min_price, max_price)
            
            # Apply all filters
            filtered_df = filtered_df[(filtered_df["discount_numeric"] >= disc_range[0]) & (filtered_df["discount_numeric"] <= disc_range[1])]
            filtered_df = filtered_df[(filtered_df["distance_numeric"] >= dist_range[0]) & (filtered_df["distance_numeric"] <= dist_range[1])]
            filtered_df = filtered_df[(filtered_df["price_numeric"] >= price_range[0]) & (filtered_df["price_numeric"] <= price_range[1])]
            
            st.divider()  # Visual separator between filters and sort

            # Initialize session state for sorting
            if 'sort_by' not in st.session_state:
                st.session_state.sort_by = 'price'
            if 'sort_asc' not in st.session_state:
                st.session_state.sort_asc = True
            
            # Sorting controls in 2x2 grid layout
            st.write("### 📊 Sort Options")
            
            col_names = ["distance", "price", "discount", "rating"]
            col_labels = ["📍 Distance", "💰 Price", "💸 Discount", "⭐ Rating"]
            
            # First row: Distance | Price (with gap spacing)
            sort_row1 = st.columns([1, 0.1, 1])
            for idx, (col_name, col_label) in enumerate(zip(col_names[:2], col_labels[:2])):
                with sort_row1[idx * 2]:  # Use index 0 and 2 (skip 1 for gap)
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
                        st.rerun()
            
            # Second row: Discount | Rating (with gap spacing)
            sort_row2 = st.columns([1, 0.1, 1])
            for idx, (col_name, col_label) in enumerate(zip(col_names[2:], col_labels[2:])):
                with sort_row2[idx * 2]:  # Use index 0 and 2 (skip 1 for gap)
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
                        st.rerun()
            
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

            st.divider()  # Visual separator between sort options and results table
            
            # Create a professional-looking table with custom styling
            st.write("### Your Matched Hotels")
            st.write(f"Sorted by: **{st.session_state.sort_by}** {'⬆️ Ascending' if st.session_state.sort_asc else '⬇️ Descending'}")
            # Drop the temporary columns before display
            cols_to_drop = ["brand_key", "price_numeric", "distance_numeric", "discount_numeric", "reviews_numeric", "retail_price_numeric"]
            # Also drop rating_value if it exists
            if "rating_value" in filtered_df.columns:
                cols_to_drop.append("rating_value")
            display_df = filtered_df.drop(columns=cols_to_drop)
            
            # Add custom CSS for a black table background with white text, frozen header and first column
            st.markdown("""
            <style>
            .table-wrapper {
                max-height: 900px;
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
                padding: 3px 10px;
                text-align: center;
                font-weight: 300;
                text-transform: uppercase;
                font-size: 10px;
                letter-spacing: 0.3px;
                color: #fff;
                background: #222;
                position: sticky;
                top: 0;
                z-index: 4;
            }
            /* Freeze first column (name) - sticky positioning */
            .hotel-table th:first-child,
            .hotel-table td:first-child {
                position: sticky;
                left: 0;
                z-index: 5;
                background: #111;
                font-weight: 600;
                width: 100px; /* Fixed narrow width so other columns are visible */
                min-width: 100px;
                max-width: 100px;
                white-space: normal; /* Allow text wrapping in name column */
                word-break: break-word;
                overflow-wrap: break-word;
            }
            .hotel-table th:first-child {
                background: #222;
                z-index: 15;
            }
            .hotel-table td {
                padding: 6px 8px;
                border-bottom: 1px solid #222;
                vertical-align: middle;
                text-align: center;
                color: #fff;
                background-color: #111;
                white-space: nowrap;
                font-size: 12px;
            }
            .hotel-table tr:last-child td {
                border-bottom: none;
            }
            .hotel-table tr:hover td {
                background-color: #222 !important;
            }
            .hotel-table a {
                display: inline-block;
                padding: 4px 8px;
                color: white;
                text-decoration: none;
                border-radius: 4px;
                font-weight: 500;
                transition: all 0.2s ease;
                font-size: 12px;
            }
            .hotel-table a:hover {
                opacity: 0.9;
                box-shadow: 0 2px 5px rgba(0, 0, 0, 0.2);
            }
            /* Add specific styling for important columns */
            .hotel-table td:nth-child(4) {
                font-weight: 700;
                color: #47ffb2; /* Price in green (bright for dark bg) */
            }
            .hotel-table td:nth-child(5) {
                font-weight: 300;
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
                    width: 80px !important; /* Smaller on mobile */
                    min-width: 80px !important;
                    max-width: 80px !important;
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
                    width: 70px !important;
                    min-width: 70px !important;
                    max-width: 70px !important;
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

 
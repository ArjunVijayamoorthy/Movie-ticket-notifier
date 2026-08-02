import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime

# ==========================================
# 1. HELPER FUNCTIONS FOR NOTIFICATIONS
# ==========================================

def send_telegram_notification(text: str):
    """Sends HTML formatted notifications to your Telegram chat."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        print("⚠️ Telegram secrets missing. Skipping Telegram notification.")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req):
            print("⚡ Telegram alert sent successfully!")
    except Exception as e:
        print(f"❌ Failed to send Telegram notification: {e}")


def send_email_notification(subject: str, html_content: str):
    """Sends HTML email notifications via Resend (Optional backup)."""
    api_key = os.getenv("RESEND_API_KEY")
    from_email = os.getenv("RESEND_FROM_EMAIL")
    to_email = os.getenv("RESEND_TO_EMAIL")

    if not api_key or not from_email or not to_email:
        print("⚠️ Resend credentials missing. Skipping email notification.")
        return

    url = "https://api.resend.com/emails"
    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "html": html_content
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(req):
            print("📧 Resend Email sent successfully!")
    except Exception as e:
        print(f"❌ Failed to send Resend email: {e}")


# ==========================================
# 2. BOOKMYSHOW DATA SCRAPING & PARSING
# ==========================================

def parse_bms_url(url: str):
    """Extracts Event Code and Region Code from the BMS URL."""
    match_event = re.search(r'(ET\d+)', url)
    event_code = match_event.group(1) if match_event else None

    parts = url.split('/')
    region_code = "CHEN"  # Default
    if "movies" in parts:
        idx = parts.index("movies")
        if idx + 1 < len(parts):
            region_code = parts[idx + 1].upper()[:4]

    return event_code, region_code


def fetch_showtimes(event_code: str, region_code: str, dates: list, theatre_filter: str, time_filter: str):
    """Hits BookMyShow's API endpoints for showtime details."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://in.bookmyshow.com",
        "Referer": "https://in.bookmyshow.com/"
    }

    all_shows = []
    
    for date_str in dates:
        if not date_str:
            continue

        api_url = f"https://in.bookmyshow.com/serv/getData?cmd=GETSHOWTIMESBYEVENTANDDATE&f=json&dc={date_str}&vc={region_code}&eid={event_code}"
        
        req = urllib.request.Request(api_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
                
                # Parse BookMyShow json structure
                if "BookMyShow" in data and "arrVenue" in data["BookMyShow"]:
                    venues = data["BookMyShow"]["arrVenue"]
                    for venue in venues:
                        venue_name = venue.get("VenueName", "")
                        
                        # Apply theatre filter if specified
                        if theatre_filter and not any(t.strip().lower() in venue_name.lower() for t in theatre_filter.split(',')):
                            continue

                        for show in venue.get("ShowTimes", []):
                            show_time = show.get("ShowTime", "")
                            
                            # Filter by time period if specified (morning, afternoon, evening, night)
                            if time_filter:
                                hour = int(show.get("ShowTimeCode", "0")[:2]) if show.get("ShowTimeCode") else 12
                                match_time = False
                                for t_cond in time_filter.split(','):
                                    t_cond = t_cond.strip().lower()
                                    if t_cond == "morning" and 6 <= hour < 12: match_time = True
                                    elif t_cond == "afternoon" and 12 <= hour < 16: match_time = True
                                    elif t_cond == "evening" and 16 <= hour < 19: match_time = True
                                    elif t_cond == "night" and (19 <= hour <= 24 or hour < 6): match_time = True
                                if not match_time:
                                    continue

                            categories = [cat.get("Price", "") for cat in show.get("Categories", [])]
                            price_info = f"₹{categories[0]}" if categories else ""

                            all_shows.append({
                                "id": f"{venue_name}_{date_str}_{show_time}",
                                "venue": venue_name,
                                "date": date_str,
                                "time": show_time,
                                "price": price_info,
                                "status": show.get("ShowBookingOptions", "Available")
                            })
        except Exception as e:
            print(f"Error fetching data for date {date_str}: {e}")

    return all_shows


# ==========================================
# 3. STATE MANAGEMENT & MAIN LOGIC
# ==========================================

def load_state(filename="bms_state.json"):
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state, filename="bms_state.json"):
    with open(filename, "w") as f:
        json.dump(state, f, indent=2)


def main():
    bms_url = os.getenv("BMS_URL")
    if not bms_url:
        print("❌ BMS_URL variable missing. Exiting.")
        return

    dates_var = os.getenv("BMS_DATES", "")
    dates = [d.strip() for d in dates_var.split(",") if d.strip()]
    
    theatre_filter = os.getenv("BMS_THEATRE", "")
    time_filter = os.getenv("BMS_TIME", "")

    event_code, region_code = parse_bms_url(bms_url)
    
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] BMS Ticket Checker - CI mode")
    print(f"Event: {event_code}  Region: {region_code}  Dates: {dates}")

    current_shows = fetch_showtimes(event_code, region_code, dates, theatre_filter, time_filter)
    previous_state = load_state()

    current_state = {show["id"]: show for show in current_shows}
    
    # Identify new showtimes or updates
    changes = []
    for show_id, show in current_state.items():
        if show_id not in previous_state:
            changes.append(f"<b>NEW:</b> {show['venue']} - {show['time']} [{show['date']}] ({show['price']})")
        elif previous_state[show_id]["status"] != show["status"]:
            changes.append(f"<b>STATUS CHANGE:</b> {show['venue']} - {show['time']} [{show['date']}] -> {show['status']}")

    # Save updated state
    save_state(current_state)

    # Dispatch Telegram Notifications
    if current_shows:
        if changes:
            message_lines = [
                "<b>🎟️ BOOKMYSHOW ALERT: TICKETS / SHOWTIMES UPDATED!</b>\n",
                f"<b>Movie/Event:</b> {event_code}",
                f"<b>Total Shows Found:</b> {len(current_shows)}\n",
                "<b>Changes Detected:</b>"
            ]
            for change in changes:
                message_lines.append(f"• {change}")
            
            message_lines.append(f"\n🔗 <a href='{bms_url}'>Book on BookMyShow</a>")
            send_telegram_notification("\n".join(message_lines))
        else:
            send_telegram_notification(
                f"<b>ℹ️ BMS Hourly Status Check:</b>\n\n"
                f"• <b>Status:</b> Showtimes Active ({len(current_shows)} found)\n"
                f"• <b>Changes:</b> No new shows or seat updates detected.\n"
                f"• <b>Monitored Dates:</b> {', '.join(dates)}"
            )
    else:
        send_telegram_notification(
            f"<b>❌ BMS Hourly Status Check:</b>\n\n"
            f"• <b>Status:</b> No showtimes or tickets are currently available.\n"
            f"• <b>Monitored Dates:</b> {', '.join(dates)}"
        )


if __name__ == "__main__":
    main()

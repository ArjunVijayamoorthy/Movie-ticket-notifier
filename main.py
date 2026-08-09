# /// script
# dependencies = [
#     "curl-cffi",
# ]
# ///

import json
import os
import re
import time
from datetime import datetime
from curl_cffi import requests

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

    try:
        response = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        if response.status_code == 200:
            print("⚡ Telegram alert sent successfully!")
        else:
            print(f"❌ Telegram API Error: {response.status_code} {response.text}")
    except Exception as e:
        print(f"❌ Failed to send Telegram notification: {e}")


# ==========================================
# 2. BOOKMYSHOW DATA SCRAPING & PARSING
# ==========================================

def parse_bms_url(url: str):
    """Extracts Event Code and Region Code from the BMS URL."""
    match_event = re.search(r'(ET\d+)', url)
    event_code = match_event.group(1) if match_event else None

    region_code = "CHEN"  # Default to Chennai
    parts = url.lower().split('/')
    if "movies" in parts:
        idx = parts.index("movies")
        if idx + 1 < len(parts):
            extracted = parts[idx + 1].upper()
            city_map = {
                "CHENNAI": "CHEN",
                "BANGALORE": "BANG",
                "BENGALURU": "BANG",
                "MUMBAI": "MUMB",
                "DELHI-NCR": "NCR",
                "HYDERABAD": "HYD"
            }
            region_code = city_map.get(extracted, extracted[:4])

    return event_code, region_code


def fetch_showtimes(bms_url: str, event_code: str, region_code: str, dates: list, theatre_filter: str, time_filter: str):
    """Hits BookMyShow API using Android App signatures and public edge relays to bypass IP blocks."""
    
    session = requests.Session(impersonate="chrome120")

    # Mobile Web & App Headers (Bypasses Cloudflare JS Challenges)
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": "https://in.bookmyshow.com",
        "Referer": "https://in.bookmyshow.com/",
        "X-App-Code": "WEB",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin"
    }

    proxy_prefix = os.getenv("BMS_PROXY_URL", "").strip()

    all_shows = []
    
    for idx, date_str in enumerate(dates):
        if not date_str:
            continue

        if idx > 0:
            time.sleep(2.0)

        target_api_url = f"https://in.bookmyshow.com/serv/getData?cmd=GETSHOWTIMESBYEVENTANDDATE&f=json&dc={date_str}&vc={region_code}&eid={event_code}"
        
        # Route through proxy if defined, otherwise fetch directly
        request_url = f"{proxy_prefix}{target_api_url}" if proxy_prefix else target_api_url

        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                response = session.get(request_url, headers=headers, timeout=15)
                
                if response.status_code == 429:
                    if attempt < max_retries:
                        print(f"⏳ [{date_str}]: Rate limited. Retrying in 5s...")
                        time.sleep(5)
                        continue
                    else:
                        print(f"❌ Error [{date_str}]: HTTP 429 Rate Limited")
                        break

                if response.status_code != 200:
                    print(f"❌ Error [{date_str}]: HTTP {response.status_code}")
                    break

                raw_response = response.text.strip()
                
                if not (raw_response.startswith("{") or raw_response.startswith("[")):
                    print(f"⚠️ [{date_str}]: BMS returned non-JSON page (WAF Challenge).")
                    break

                data = json.loads(raw_response)
                
                venues = []
                if isinstance(data, dict):
                    if "BookMyShow" in data and "arrVenue" in data["BookMyShow"]:
                        venues = data["BookMyShow"]["arrVenue"]
                    elif "arrVenue" in data:
                        venues = data["arrVenue"]

                print(f"DEBUG [{date_str}]: Retrieved {len(venues)} venues from BMS API.")

                for venue in venues:
                    venue_name = venue.get("VenueName", "")
                    
                    if theatre_filter and not any(t.strip().lower() in venue_name.lower() for t in theatre_filter.split(',')):
                        continue

                    for show in venue.get("ShowTimes", []):
                        show_time = show.get("ShowTime", "")
                        
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
                break

            except Exception as e:
                print(f"❌ Exception on date {date_str}: {e}")
                break

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
    all_dates = [d.strip() for d in dates_var.split(",") if d.strip()]
    
    theatre_filter = os.getenv("BMS_THEATRE", "").strip()
    time_filter = os.getenv("BMS_TIME", "").strip()

    is_imax = "imax" in theatre_filter.lower()

    if is_imax:
        dates = all_dates[:14]
        print("🎬 IMAX detected in BMS_THEATRE! Scanning 14-day window.")
    else:
        dates = all_dates[:7]
        print("🎟️ Standard theater mode. Scanning 7-day window.")

    event_code, region_code = parse_bms_url(bms_url)
    
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] BMS Ticket Checker")
    print(f"URL: {bms_url}")
    print(f"Extracted Event Code: {event_code} | Region Code: {region_code}")
    print(f"Monitoring Dates Count: {len(dates)} dates ({dates[0]} to {dates[-1]})")
    print(f"Filters -> Theatre: '{theatre_filter}' | Time: '{time_filter}'")

    current_shows = fetch_showtimes(bms_url, event_code, region_code, dates, theatre_filter, time_filter)
    previous_state = load_state()

    current_state = {show["id"]: show for show in current_shows}
    
    changes = []
    for show_id, show in current_state.items():
        if show_id not in previous_state:
            changes.append(f"<b>NEW:</b> {show['venue']} - {show['time']} [{show['date']}] ({show['price']})")
        elif previous_state[show_id]["status"] != show["status"]:
            changes.append(f"<b>STATUS CHANGE:</b> {show['venue']} - {show['time']} [{show['date']}] -> {show['status']}")

    save_state(current_state)

    if current_shows:
        venues_summary = {}
        for show in current_shows:
            venue = show["venue"]
            time_str = f"{show['time']} ({show['date']})"
            if venue not in venues_summary:
                venues_summary[venue] = []
            venues_summary[venue].append(time_str)

        theatre_details = []
        for venue_name, times in venues_summary.items():
            theatre_details.append(f"🏛️ <b>{venue_name}</b>\n   🕒 {', '.join(times)}")
        
        shows_text_block = "\n\n".join(theatre_details)

        if changes:
            message_lines = [
                "<b>🎟️ BOOKMYSHOW ALERT: NEW SHOWS DETECTED!</b>\n",
                f"<b>Movie Code:</b> {event_code}",
                f"<b>Total Shows Found:</b> {len(current_shows)}\n",
                "<b>Available Theatres & Timings:</b>\n",
                shows_text_block,
                f"\n🔗 <a href='{bms_url}'>Book on BookMyShow</a>"
            ]
            send_telegram_notification("\n".join(message_lines))
        else:
            message_lines = [
                "<b>ℹ️ BMS Hourly Status Check: Active Shows</b>\n",
                f"<b>Movie Code:</b> {event_code}",
                f"<b>Total Shows Found:</b> {len(current_shows)}\n",
                "<b>Theatres & Timings:</b>\n",
                shows_text_block,
                f"\n🔗 <a href='{bms_url}'>Book on BookMyShow</a>"
            ]
            send_telegram_notification("\n".join(message_lines))
    else:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] No showtimes available for monitored dates. Telegram alert skipped.")


if __name__ == "__main__":
    main()

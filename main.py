import json
import os
import re
import time
import urllib.parse
import urllib.request
import http.cookiejar
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


def fetch_showtimes(event_code: str, region_code: str, dates: list, theatre_filter: str, time_filter: str):
    """Hits BookMyShow API with cookie persistence, exponential backoff, and request pacing."""
    
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://in.bookmyshow.com",
        "Referer": f"https://in.bookmyshow.com/buytickets/{event_code}",
        "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin"
    }

    all_shows = []
    
    for idx, date_str in enumerate(dates):
        if not date_str:
            continue

        # Safe request pacing (1.5 seconds between date calls)
        if idx > 0:
            time.sleep(1.5)

        api_url = f"https://in.bookmyshow.com/serv/getData?cmd=GETSHOWTIMESBYEVENTANDDATE&f=json&dc={date_str}&vc={region_code}&eid={event_code}"
        
        req = urllib.request.Request(api_url, headers=headers)
        
        # Retry loop for HTTP 429 rate limits
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                with opener.open(req, timeout=15) as response:
                    raw_response = response.read().decode("utf-8")
                    
                    if not raw_response.strip().startswith("{") and not raw_response.strip().startswith("["):
                        print(f"⚠️ [{date_str}]: BMS returned non-JSON response (WAF challenge or no shows).")
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
                    break  # Success break out of retry loop

            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < max_retries:
                    print(f"⏳ [{date_str}]: HTTP 429 Rate limited. Retrying in 5 seconds...")
                    time.sleep(5)
                else:
                    print(f"❌ Error fetching data for date {date_str}: HTTP {e.code} {e.reason}")
                    break
            except Exception as e:
                print(f"❌ Error fetching data for date {date_str}: {e}")
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
    
    theatre_filter = os.getenv("BMS_THEATRE", "")
    time_filter = os.getenv("BMS_TIME", "")

    is_imax = "imax" in theatre_filter.lower()

    # Cap at 14 days maximum to stay below BMS request rate-limits per execution
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

    current_shows = fetch_showtimes(event_code, region_code, dates, theatre_filter, time_filter)
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

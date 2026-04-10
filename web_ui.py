import os
import asyncio
import logging
import sqlite3
import httpx
import re
import urllib.parse
from datetime import datetime
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
from pydantic import BaseModel
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn

# Load environment variables
load_dotenv()

# Configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PROXY_URL = os.getenv("PROXY_URL")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 45))
VINTED_API_URL = "https://www.vinted.co.uk/api/v2/catalog/items"

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Basic in-memory state
class BotState:
    running = False
    task: asyncio.Task = None
    alerts_sent = 0
    min_price: float = 0.0
    max_price: float = 100.0
    buffer_mins: int = 20
    next_refresh_ts: float = 0.0
    is_scraping: bool = False
    force_scrape: bool = False
    first_run_cycles = {} # Track which queries have been primed
    recent_gems = [] # Track recent alerts for the UI
    search_queries = [
        "Vintage Ring",
        "9ct gold",
        "opal ring vintage",
        "job lot jewellery",
        "vintage jewellery collection",
        "Antique Jewellery",
        "old ring",
        "vintage jewellery job lot",
        "gold stech bracelet",
        "shell bag vintage",
        "art deco ring",
        "9ct old ring"
    ]
    negative_filters = []

    @property
    def grouped_queries(self):
        grouped = {}
        for q in self.search_queries:
            parts = q.split(' ', 1)
            is_url = parts[0].startswith("http") and "vinted" in parts[0]
            if is_url:
                link = parts[0]
                tag = parts[1] if len(parts) > 1 else ""
            else:
                link = "Standalone Keywords"
                tag = q
            
            if link not in grouped:
                grouped[link] = []
            grouped[link].append({"full": q, "tag": tag})
        return grouped

state = BotState()

# FastAPI App
app = FastAPI()

# Make templates directory
os.makedirs("templates", exist_ok=True)
templates = Jinja2Templates(directory="templates")

def init_db():
    conn = sqlite3.connect("items.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seen_items (
            item_id TEXT PRIMARY KEY,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_item_seen(item_id: str) -> bool:
    conn = sqlite3.connect("items.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (str(item_id),))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def mark_item_seen(item_id: str):
    conn = sqlite3.connect("items.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO seen_items (item_id) VALUES (?)", (str(item_id),))
    conn.commit()
    conn.close()

def meets_criteria(item: dict, search_term: str) -> bool:
    try:
        price_data = item.get("price")
        if isinstance(price_data, dict):
            price_str = price_data.get("amount", "0")
        else:
            price_str = str(price_data)
        price = float(price_str)
    except ValueError:
        return False

    title = item.get("title", "").lower()
    description = item.get("description", "").lower()
    text_content = f"{title} {description}"

    # Negative Filter Check
    for neg in state.negative_filters:
        if neg.lower() in text_content:
            return False

    # Positive Checks
    term_lower = search_term.lower()

    # Global Price Range Check
    if price < state.min_price or price > state.max_price:
        return False
    
    # Bundle type constraints
    bundle_terms = ["job lot", "bundle", "spares and repairs", "mixed lot"]
    if any(bt in term_lower for bt in bundle_terms):
        return price < 40.0 # Increase price a little for bundles

    # Precious metals type constraints
    metal_terms = ["gold", "silver", "9ct", ".925", "hallmarked", "antique"]
    if any(mt in term_lower for mt in metal_terms):
        # We want cheap precious metals!
        return price < 30.0 

    return True

def escape_markdown(text: str) -> str:
    escape_chars = r"_*[]()~`>#+-=|{}.!"
    if not text:
        return ""
    for char in escape_chars:
        text = text.replace(char, f"\\{char}")
    return text

async def send_telegram_alert(bot: Bot, item: dict, query_tag: str, send_msg: bool = True):
    title = escape_markdown(item.get("title", "No Title"))
    
    price_data = item.get("price")
    if isinstance(price_data, dict):
        price = escape_markdown(str(price_data.get("amount", "0")))
        currency = escape_markdown(price_data.get("currency_code", "£"))
    else:
        price = escape_markdown(str(price_data))
        currency = escape_markdown("£")
        
    brand = escape_markdown(item.get("brand_title", "Unknown Brand"))
    url = escape_markdown(item.get("url", ""))

    # Attempt to extract publish date if available
    publish_str = "Unknown"
    created_ts = item.get("created_at_ts")
    if not created_ts and "photo" in item and "high_resolution" in item["photo"]:
        created_ts = item["photo"]["high_resolution"].get("timestamp")
        
    if created_ts:
        try:
            from datetime import timezone
            # Sometimes Vinted provides timestamps in ms instead of s
            if created_ts > 9999999999: created_ts /= 1000
            publish_str = escape_markdown(datetime.fromtimestamp(created_ts, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'))
        except:
            publish_str = "Parsing Error"
    elif "photo" in item and "high_resolution" in item["photo"]:
        # Fallback: Sometimes photo upload timestamp equals item publish time
        photo_ts = item["photo"]["high_resolution"].get("timestamp")
        if photo_ts:
            try:
                from datetime import timezone
                publish_str = escape_markdown(datetime.fromtimestamp(photo_ts, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'))
            except:
                pass


    message = (
        f"🚨 *Vinted Gem Alert*\n\n"
        f"🏷 *Title:* {title}\n"
        f"💰 *Price:* {price} {currency}\n"
        f"🏢 *Brand:* {brand}\n"
        f"⏳ *Published:* {publish_str}\n\n"
        f"🔗 [View Item]({url})"
    )

    photo_url = ""
    if "photo" in item and "url" in item["photo"]:
        photo_url = item["photo"]["url"]

    # Calculate actual timestamp and numeric price for sorting/frontend relative time
    item_ts = 0.0
    ct = item.get("created_at_ts")
    pt = item.get("photo", {}).get("high_resolution", {}).get("timestamp")
    # Take whichever is valid
    if ct:
        item_ts = ct / 1000 if ct > 9999999999 else float(ct)
    elif pt:
        item_ts = float(pt)
        
    num_price = 0.0
    if isinstance(price_data, dict):
        try: num_price = float(price_data.get("amount", 0))
        except: pass
    else:
        try: num_price = float(str(price_data))
        except: pass

    user = item.get("user", {})
    
    raw_rating = user.get("feedback_reputation")
    review_count = user.get("feedback_count", 0)
    if raw_rating is not None:
        try:
            val = float(raw_rating)
            star_val = round(val * 5, 1) if val <= 1.0 else round(val, 1)
            seller_stars = f"⭐ {star_val} ({review_count})"
        except:
            seller_stars = f"⭐ ? ({review_count})"
    else:
        seller_stars = "⭐ No reviews"

    desc = item.get("description", "")
    if len(desc) > 80:
        desc = desc[:77] + "..."

    # Store for UI dashboard
    new_gem = {
        "title": item.get("title", "No Title"),
        "query_tag": query_tag,
        "price": f"{price.replace('\\', '')} {currency.replace('\\', '')}",
        "price_numeric": num_price,
        "url": item.get("url", ""),
        "photo": photo_url,
        "publish_date": publish_str.replace('\\', ''),
        "timestamp": item_ts,
        "brand_title": item.get("brand_title", ""),
        "size_title": item.get("size_title", ""),
        "condition_title": item.get("condition_title", ""),
        "favourite_count": item.get("favourite_count", 0) or item.get("favorite_count", 0),
        "status": str(item.get("status") or "Available"),
        "description": desc,
        "seller_stars": seller_stars
    }
    state.recent_gems.insert(0, new_gem)
    state.recent_gems = state.recent_gems[:50] # Keep last 50
    
    if send_msg and bot:
        state.alerts_sent += 1
        try:
            await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN_V2)
            logger.info(f"Alert sent for item {item.get('id')}")
        except TelegramError as e:
            logger.error(f"Failed to send Telegram message: {e}")

async def fetch_vinted_items(client: httpx.AsyncClient, search_term: str) -> list:
    import urllib.parse
    
    search_term = search_term.strip()
    parts = search_term.split(" ", 1)
    url_part = parts[0]
    extra_search = parts[1] if len(parts) > 1 else None
    
    if url_part.startswith("http") and "vinted" in url_part:
        # If user pasted a Vinted URL (e.g. category filter), parse its parameters or path
        parsed_url = urllib.parse.urlparse(url_part)
        query_string = parsed_url.query
        
        # parse_qsl gives us a list of tuples, which httpx handles correctly for arrays like catalog[]
        raw_params = urllib.parse.parse_qsl(query_string)
        params = []
        for k, v in raw_params:
            if k in ("catalog[]", "catalog_id[]"): k = "catalog_ids[]"
            elif k in ("brand_id[]", "brand[]"): k = "brand_ids[]"
            elif k in ("size_id[]", "size[]"): k = "size_ids[]"
            elif k in ("material_id[]", "material[]"): k = "material_ids[]"
            elif k in ("color_id[]", "color[]"): k = "color_ids[]"
            elif k in ("status[]", "status_id[]"): k = "status_ids[]"
            params.append((k, v))
            
        # Try to extract catalog ID from the URL path (e.g. /catalog/21-jewellery)
        path_match = re.search(r'/catalog/(\d+)', parsed_url.path)
        if path_match and not any(k == "catalog_ids[]" for k, v in params):
            params.append(("catalog_ids[]", path_match.group(1)))
            
        # Extract direct search text if present in URL
        if extra_search:
            # Overwrite or add search_text with exactly what the user typed after the URL space
            params = [(k, v) for k, v in params if k != "search_text"]
            params.append(("search_text", extra_search))
        elif "search_text" not in [k for k, v in params]:
            search_match = re.search(r'search_text=([^&]+)', query_string)
            if search_match:
                params.append(("search_text", urllib.parse.unquote(search_match.group(1))))

        # Add order if not present
        if not any(k == "order" for k, v in params):
            params.append(("order", "newest_first"))
    else:
        params = {"search_text": search_term, "order": "newest_first"}

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Referer": "https://www.vinted.co.uk/"
    }

    try:
        response = await client.get(VINTED_API_URL, params=params, headers=headers)
        
        if response.status_code in (401, 403):
            logger.warning(f"{response.status_code} Unauthorized/Forbidden: Missing valid session cookie. Attempting to refresh cookie...")
            await client.get("https://www.vinted.co.uk/", headers={
                "User-Agent": headers["User-Agent"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8"
            })
            response = await client.get(VINTED_API_URL, params=params, headers=headers)
            
            if response.status_code in (401, 403):
                logger.warning("Still 403. Render's IP might be blocked by Cloudflare, or headers need further refinement.")
                return []
                
        if response.status_code == 429:
            logger.warning("429 Too Many Requests: Rate limited.")
            await asyncio.sleep(60)
            return []
            
        response.raise_for_status()
        data = response.json()
        return data.get("items", [])
        
    except httpx.RequestError as e:
        logger.error(f"Network error while fetching '{search_term}': {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error fetching '{search_term}': {e}")
        return []

async def monitor_loop():
    init_db()
    
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("YOUR_"):
        logger.warning("No valid TELEGRAM_TOKEN. Running in DRY-RUN mode.")
        bot = None
    else:
        bot = Bot(token=TELEGRAM_TOKEN)
    
    proxy_mounts = None
    if PROXY_URL and not PROXY_URL.startswith("http://username"):
        proxy_mounts = {
            "http://": httpx.AsyncHTTPTransport(proxy=PROXY_URL),
            "https://": httpx.AsyncHTTPTransport(proxy=PROXY_URL)
        }

    async with httpx.AsyncClient(mounts=proxy_mounts, follow_redirects=True) as client:
        try:
            logger.info("Fetching initial session cookies from Vinted homepage...")
            await client.get("https://www.vinted.co.uk/", headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8"
            })
        except Exception as e:
            logger.warning(f"Failed to get initial cookies: {e}")

        while state.running:
            state.is_scraping = True
            state.next_refresh_ts = 0.0
            logger.info("Starting new polling cycle...")
            # Work on a copy of search queries in case UI changes them mid-flight
            queries_to_run = list(state.search_queries)
            
            sem = asyncio.Semaphore(2) # Max 2 concurrent HTTP requests to Vinted
            
            async def process_query(query):
                async with sem:
                    if not state.running:
                        return
                    
                    # Check if this query needs to be explicitly "primed" 
                    # (i.e., we skip alerting for the first page since these are older results)
                    is_first_run_for_query = state.first_run_cycles.get(query, True)
                    
                    if is_first_run_for_query:
                        logger.info(f"Priming: {query} (Will not send alerts on this cycle)")
                    else:
                        logger.info(f"Searching for: {query}")
                    
                    items = await fetch_vinted_items(client, query)
                    
                    for item in items:
                        item_id = str(item.get("id"))
                        if not item_id or is_item_seen(item_id):
                            continue
                        
                        # Enforce strict freshness (< 20 mins)
                        # Use created_at_ts to avoid seeing old items bumped to the top
                        item_ts = item.get("created_at_ts")
                        if not item_ts and "photo" in item and "high_resolution" in item["photo"]:
                            item_ts = item["photo"]["high_resolution"].get("timestamp")
                        
                        is_fresh = False
                        if item_ts:
                            try:
                                ts_val = float(item_ts)
                                if ts_val > 9999999999: 
                                    ts_val /= 1000
                                from datetime import datetime, timezone
                                time_diff = datetime.now(timezone.utc).timestamp() - ts_val
                                if -600 <= time_diff <= (state.buffer_mins * 60): # strictly within last buffer_mins
                                    is_fresh = True
                            except:
                                pass
                                
                        if not is_fresh:
                            mark_item_seen(item_id)
                            continue
                        
                        if meets_criteria(item, query):
                            logger.info(f"Item met criteria! (First run: {is_first_run_for_query}) ID: {item_id}")
                            # Do not send Telegram alerts on the very first run (priming), but add to UI
                            await send_telegram_alert(bot, item, query, send_msg=not is_first_run_for_query)
                        mark_item_seen(item_id)
                    
                    state.first_run_cycles[query] = False

            # Execute queries sequentially with longer delays to avoid IP bans
            import random
            for query in queries_to_run:
                if not state.running: break
                await process_query(query)
                # Sleep aggressively between 5 and 10 seconds between searches
                await asyncio.sleep(random.uniform(5.5, 9.5))
                
            logger.info(f"Cycle complete. Sleeping for {POLL_INTERVAL} seconds.")
            state.is_scraping = False
            from datetime import datetime, timezone
            state.next_refresh_ts = datetime.now(timezone.utc).timestamp() + POLL_INTERVAL
            for _ in range(POLL_INTERVAL):
                if not state.running or state.force_scrape:
                    break
                await asyncio.sleep(1)
            state.next_refresh_ts = 0.0
            state.force_scrape = False

# API Endpoints
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={"request": request, "state": state}
    )

@app.post("/start")
async def start_bot():
    if not state.running:
        state.running = True
        state.task = asyncio.create_task(monitor_loop())
    return {"message": "Started", "running": state.running}

@app.post("/stop")
async def stop_bot():
    state.running = False
    if state.task:
        state.task.cancel()
    state.first_run_cycles.clear() # Reset priming on stop
    return {"message": "Stopped", "running": state.running}

@app.post("/api/queries")
async def add_query(query: str = Form(...)):
    if query and query not in state.search_queries:
        state.search_queries.append(query)
    return {"queries": state.search_queries}

@app.post("/api/queries/replace")
async def replace_link_tags(link: str = Form(...), tags: str = Form("")):
    link = link.strip()
    tag_str = tags.strip()
    
    # Remove old queries for this exact link
    if link == "Standalone Keywords":
        # Keep URLs, discard all "Standalone Keywords" (which are queries with no http link)
        state.search_queries = [q for q in state.search_queries if q.startswith("http")]
    else:
        # Remove anything starting with this link
        state.search_queries = [q for q in state.search_queries if not q.startswith(link)]
    
    # Add newly mapped queries back
    if tag_str:
        # User defined tags separated by comma, so loop them as OR checks
        split_tags = [t.strip() for t in tag_str.split(',')]
        for valid_tag in split_tags:
            if not valid_tag: continue
            
            if link == "Standalone Keywords":
                if valid_tag not in state.search_queries:
                    state.search_queries.append(valid_tag)
            else:
                new_q = f"{link} {valid_tag}"
                if new_q not in state.search_queries:
                    state.search_queries.append(new_q)
    else:
        # If they emptied tags completely but meant to keep the link alone:
        if link != "Standalone Keywords" and link not in state.search_queries:
            state.search_queries.append(link)

    return {"status": "replaced"}

@app.post("/api/queries/remove")
async def remove_query(query: str = Form(...)):
    if query in state.search_queries:
        state.search_queries.remove(query)
    return {"queries": state.search_queries}

@app.post("/api/queries/clear")
async def clear_queries():
    state.search_queries.clear()
    return {"queries": state.search_queries}

@app.post("/api/queries/refresh")
async def force_refresh():
    state.force_scrape = True
    return {"status": "refreshing"}

@app.post("/api/gems/clear")
async def clear_gems():
    state.recent_gems.clear()
    state.alerts_sent = 0
    return {"status": "cleared"}

@app.post("/api/price")
async def update_settings(min_price: float = Form(...), max_price: float = Form(...), buffer_mins: int = Form(30)):
    state.min_price = min_price
    state.max_price = max_price
    state.buffer_mins = buffer_mins
    return {"min_price": state.min_price, "max_price": state.max_price, "buffer_mins": state.buffer_mins}

@app.get("/api/state")
async def get_state():
    return {
        "running": state.running,
        "recent_gems": state.recent_gems,
        "alerts_sent": state.alerts_sent,
        "next_refresh_ts": state.next_refresh_ts,
        "is_scraping": state.is_scraping
    }

@app.post("/api/negatives")
async def add_negative(neg: str = Form(...)):
    if neg and neg not in state.negative_filters:
        state.negative_filters.append(neg.lower())
    return {"negatives": state.negative_filters}

@app.post("/api/negatives/remove")
async def remove_negative(neg: str = Form(...)):
    if neg in state.negative_filters:
        state.negative_filters.remove(neg)
    return {"negatives": state.negative_filters}

if __name__ == "__main__":
    uvicorn.run("web_ui:app", host="0.0.0.0", port=8000, reload=True)
# Vintage Bot: Asynchronous Vinted Sniper & Dashboard

Vintage Bot is a fully asynchronous, high-performance web scraper and real-time dashboard designed explicitly for Vinted. It continuously scours the Vinted API across custom keyword lists to find hidden gems, filters them according to your custom pre-defined constraints, and acts as a sniping companion by instantly sending out Telegram alerts when matching items are found. 

It comes with a fully-featured, dynamically updating front-end web dashboard to manage your search scopes, track prices, discard unwanted results, and keep a real-time log of the latest finds.

---

## 🎯 Core Features

### ⚡ Blazing Fast Asynchronous Scraping Engine
- **Parallel Query Processing**: Uses `asyncio.Semaphore` and `asyncio.gather` to scrape multiple keyword targets simultaneously without getting rate-limited (429 errors).
- **Smart Staggering**: Includes built-in cooldowns and staggering logic to emulate human behavior and bypass strict Cloudflare or API rate limits.
- **Continuous Background Loop**: Once the engine is activated (`bot_run`), the event loop continually probes the platform in an infinite monitor cycle, updating the central state object.

### 🎛 Real-time Web Dashboard (FastAPI & JS)
- **Live Event Handling**: Operates via a FastAPI server (`web_ui.py`) serving a responsive frontend built carefully with tailwind/vanilla JS. Features a built-in gorgeous dark mode layout out of the box to reduce eye strain.
- **Auto-Refreshing Data**: The dashboard refreshes periodically to show the next check cycle and incoming gems without requiring a manual page reload.
- **Dynamic Configuration UI**: 
  - Manage and fold/unfold long lists of **Target Keywords**.
  - Edit **Negative Keywords** to exclude specific bad listings.
  - Precise numeric input mapping for exact minimum/maximum price boundaries.

### 💎 Rich "Gem" UI Cards
When a matching item is detected, it is extracted, categorized, and pushed directly to your "Recent Gems" feed. Cards feature:
1. **Intelligent Bading**: Displays the original query that caught the item.
2. **Product Details**: Extracted data covering `brand_title`, `size_title`, and `condition_title`.
3. **Favorites Count**: Overlayed heartbeat icon showing exactly how many users favorited the item (great for deducing how quickly an item might sell).
4. **Description Previews**: Small, truncated quotes from the listing to provide further context at a glance.
5. **Seller Insights**: Parsed and mathematical reformatting of the Vinted seller's star rating scaling it out to a regular `⭐ X.X` standard combined with immediate feedback review volume. Also displays tracking elements like `status` indicating item availability.

### 🚨 Smart Telegram Integration
- Formats rich Markdown templates and sends direct alerts to you via a Telegram Bot API instance.
- Tracks `publish_date`, `url`, and real prices.
- Highly resilient exception catching to discard broken payloads without crashing the monitoring loop.

---

## 🚀 How It Works Under The Hood

1. **State Management**: A Python object (`BotState`) stays alive in the background server process collecting targets:
    ```python
    queries = ["vintage gold ring", "925 silver necklace"]
    negatives = ["plastic", "fake", "damage"]
    ```
2. **Scrape Loop**: The `web_ui` creates an `httpx.AsyncClient`. Instead of scraping linearly, `bot_run` maps each active array target into parallel requests wrapped in a constraint semaphore (max 5 concurrent requests usually).
3. **Filtering Mechanism**: Fetches the newest objects ordered by `newest_first`. Then evaluates against pricing guardrails and excludes arrays using localized logic (e.g. ignoring metals priced suspiciously low or suspiciously high).
4. **Distribution**: Found items execute `state.recent_gems.insert(0, new_gem)` appending to your live state UI list and then broadcast a Telegram summary containing inline deep links to purchase.

---

## 🛠 Tech Stack

- **Backend**: Python 3.9+, [FastAPI](https://fastapi.tiangolo.com/), [HTTPX](https://www.python-httpx.org/), built-in `asyncio`.
- **Frontend**: HTML5, Vanilla JavaScript, and heavily styled via [Tailwind CSS](https://tailwindcss.com/) components to create a sleek "Apple-like", glassy aesthetic dashboard featuring a polished Dark Mode GUI.
- **Notification Bus**: [Python-Telegram-Bot](https://python-telegram-bot.org/) API Wrapper.
- **Container / Persistence**: Internal memory states mapped loosely to standard SQLite/FileSystem operations for quick loading (`items.db` tracks historically seen IDs).

---

## 💻 Installation & Usage

1. **Clone and setup virtual environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Configure your Variables**:
   Setup your `.env` configuration file containing:
   - Your `TELEGRAM_BOT_TOKEN`.
   - Your `TELEGRAM_CHAT_ID`.
3. **Start the Engine**:
   ```bash
   python web_ui.py
   ```
4. **Access the Dashboard**:
   Open a web browser and navigate to `http://localhost:8000` to interact with your dashboard, add target queries, and begin hunting!

---

*Disclaimer: This codebase interacts closely with the Vinted API. It uses concurrency scaling algorithms to remain hidden from firewall detection headers and should be used responsibly to avoid IP/account banishment.*
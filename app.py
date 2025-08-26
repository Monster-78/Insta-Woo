#!/usr/bin/env python3
"""
WooCommerce → Instagram Auto-Poster
- Cloud-friendly (Render/Railway)
- Policy-safe pacing (anti-spam): daily cap, hourly request governor, min gap between posts
- Auto-resume next day
- Carousel batching (optional)
- SQLite queue (durable)
- Webhook endpoint for WooCommerce "Product created"

Only standard libraries + requests + Flask.  (Install both.)

ENV VARS (set in Render/Railway):
  PORT=8000                            # Render uses $PORT automatically; keep fallback
  IG_USER_ID=1784...                   # Instagram Business/Creator User ID
  IG_TOKEN=EAAG...                     # Long-lived access token with posting perms
  DAILY_POST_LIMIT=10                  # Safe default; adjust to taste
  MIN_MINUTES_BETWEEN_POSTS=30         # Spacing between published feed posts
  USE_CAROUSEL=true                    # true/false — batch up to 10 products per post
  CAROUSEL_MAX_ITEMS=10                # IG max is 10
  REQUESTS_PER_HOUR_LIMIT=500          # IG Graph API guideline
  REQUESTS_SAFETY_RATIO=0.8            # Keep at/below 80% of the hourly cap
  TIMEZONE=Asia/Kolkata                # For midnight reset
  CAPTION_TEMPLATE=🆕 {name}\nPrice: ₹{price}\nBuy: {link}\n#newarrivals #shopnow

Endpoints:
  POST /wc-webhook     ← set this URL in WooCommerce Webhooks (Product created)
  GET  /health         ← liveness check
  GET  /metrics        ← simple JSON counters

Author: ChatGPT (GPT-5 Thinking)
"""

import json
import os
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import List, Dict, Any

import requests
from flask import Flask, request, jsonify

# ------------------------ Config ------------------------

def env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}

PORT = int(os.getenv("PORT", "8000"))
IG_USER_ID = os.getenv("1497990968063965", "")
IG_TOKEN = os.getenv("EAAUITQVbWnMBPSgm35erDUi1ap0U8tvqE3rYYALeProTuZCcJEZApXAePMhkeJ8ZBa6rVUsXq544HbCLLKR65ki8GAFt9adTyRWFqhlL9WiGA9wqSOsPdsOIYFdby0qgIiZBJI0bXAKimeyAZBkRGbNdd34XtLeWZBty7EUpQPvBDMdvTwM8NLUI4DjlJ3ViGoZBPyfQXBwGuBY6CN8", "")
DAILY_POST_LIMIT = int(os.getenv("DAILY_POST_LIMIT", "10"))
MIN_MINUTES_BETWEEN_POSTS = int(os.getenv("MIN_MINUTES_BETWEEN_POSTS", "30"))
USE_CAROUSEL = env_bool("USE_CAROUSEL", True)
CAROUSEL_MAX_ITEMS = max(1, min(10, int(os.getenv("CAROUSEL_MAX_ITEMS", "10"))))  # IG hard cap
REQUESTS_PER_HOUR_LIMIT = int(os.getenv("REQUESTS_PER_HOUR_LIMIT", "500"))
REQUESTS_SAFETY_RATIO = float(os.getenv("REQUESTS_SAFETY_RATIO", "0.8"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")
CAPTION_TEMPLATE = os.getenv("CAPTION_TEMPLATE", "🆕 {name}\nPrice: ₹{price}\nBuy: {link}\n#newarrivals #shopnow")

GRAPH_BASE = "https://graph.facebook.com/v21.0"
DB_PATH = os.getenv("DB_PATH", "data.sqlite3")

# ------------------------ App / DB ------------------------

app = Flask(__name__)
_db_lock = threading.Lock()

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    price TEXT,
    link TEXT,
    images_json TEXT,
    status TEXT DEFAULT 'queued', -- queued|posted|skipped|error
    error TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    posted_at TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    d TEXT PRIMARY KEY,            -- date key in local tz (YYYY-MM-DD)
    posts_made INTEGER DEFAULT 0,
    requests_made INTEGER DEFAULT 0,
    last_post_at TEXT
);
CREATE TABLE IF NOT EXISTS req_log (
    ts TEXT                       -- UTC second resolution
);
"""


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock:
        conn = db()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()


# ------------------------ Time Helpers ------------------------

try:
    import zoneinfo  # py3.9+
except ImportError:
    zoneinfo = None


def now_local():
    # Best effort TZ handling; if zoneinfo unavailable, fallback to naive localtime
    if zoneinfo:
        tz = zoneinfo.ZoneInfo(TIMEZONE)
        return datetime.now(tz)
    return datetime.now()


def today_key() -> str:
    return now_local().strftime("%Y-%m-%d")


# ------------------------ Metrics / Rate Limits ------------------------

def incr_requests(n: int = 1):
    with _db_lock:
        conn = db()
        try:
            # log timestamps for last-hour window calc
            ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
            for _ in range(n):
                conn.execute("INSERT INTO req_log(ts) VALUES (?)", (ts,))
            # bump daily counter
            d = today_key()
            conn.execute("INSERT INTO metrics(d) VALUES (?) ON CONFLICT(d) DO NOTHING", (d,))
            conn.execute("UPDATE metrics SET requests_made = COALESCE(requests_made,0) + ? WHERE d=?", (n, d))
            conn.commit()
        finally:
            conn.close()


def requests_last_hour() -> int:
    cutoff = datetime.utcnow() - timedelta(hours=1)
    with _db_lock:
        conn = db()
        try:
            cur = conn.execute("SELECT COUNT(*) AS c FROM req_log WHERE ts >= ?", (cutoff.strftime("%Y-%m-%dT%H:%M:%S"),))
            c = cur.fetchone()[0]
            # prune old rows sometimes
            conn.execute("DELETE FROM req_log WHERE ts < ?", ((datetime.utcnow() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),))
            conn.commit()
            return int(c)
        finally:
            conn.close()


def can_make_more_requests(n_needed: int) -> bool:
    cap = int(REQUESTS_PER_HOUR_LIMIT * REQUESTS_SAFETY_RATIO)
    return (requests_last_hour() + n_needed) <= cap


def incr_posts():
    with _db_lock:
        conn = db()
        try:
            d = today_key()
            conn.execute("INSERT INTO metrics(d) VALUES (?) ON CONFLICT(d) DO NOTHING", (d,))
            conn.execute("UPDATE metrics SET posts_made = COALESCE(posts_made,0) + 1, last_post_at = ? WHERE d=?", (now_local().isoformat(), d))
            conn.commit()
        finally:
            conn.close()


def posts_today() -> int:
    with _db_lock:
        conn = db()
        try:
            cur = conn.execute("SELECT posts_made FROM metrics WHERE d=?", (today_key(),))
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()


def minutes_since_last_post() -> float:
    with _db_lock:
        conn = db()
        try:
            cur = conn.execute("SELECT last_post_at FROM metrics WHERE d=?", (today_key(),))
            row = cur.fetchone()
            if not row or not row[0]:
                return 1e9
            last = datetime.fromisoformat(row[0])
            delta = now_local() - last
            return max(0.0, delta.total_seconds()/60.0)
        finally:
            conn.close()

# ------------------------ Queue Ops ------------------------

def enqueue_item(name: str, price: str, link: str, images: List[str]):
    with _db_lock:
        conn = db()
        try:
            conn.execute(
                "INSERT INTO queue(name, price, link, images_json) VALUES (?, ?, ?, ?)",
                (name, price, link, json.dumps(images))
            )
            conn.commit()
        finally:
            conn.close()


def fetch_next_batch(max_items: int) -> List[sqlite3.Row]:
    with _db_lock:
        conn = db()
        try:
            cur = conn.execute("SELECT * FROM queue WHERE status='queued' ORDER BY id ASC LIMIT ?", (max_items,))
            rows = cur.fetchall()
            return rows
        finally:
            conn.close()


def mark_items(ids: List[int], status: str, error: str = None):
    if not ids:
        return
    with _db_lock:
        conn = db()
        try:
            for _id in ids:
                conn.execute("UPDATE queue SET status=?, error=?, posted_at=? WHERE id=?",
                             (status, error, now_local().isoformat() if status == 'posted' else None, _id))
            conn.commit()
        finally:
            conn.close()

# ------------------------ Instagram Graph API ------------------------

class IGError(Exception):
    pass


def ig_post(url: str, data: Dict[str, Any], need_requests: int = 1) -> Dict[str, Any]:
    # honor hourly safety budget
    backoff = 5
    while not can_make_more_requests(need_requests):
        time.sleep(10)
    try:
        resp = requests.post(url, data=data, timeout=30)
        incr_requests(need_requests)
    except requests.RequestException as e:
        raise IGError(f"network error: {e}")

    if resp.status_code >= 400:
        # IG can return 4xx for permissions/format; 5xx transient
        raise IGError(f"HTTP {resp.status_code}: {resp.text}")
    try:
        return resp.json()
    except Exception:
        raise IGError(f"non-JSON response: {resp.text[:200]}")


def create_photo_container(image_url: str, caption: str = None, is_carousel_item: bool = False) -> str:
    data = {"image_url": image_url, "access_token": IG_TOKEN}
    if caption:
        data["caption"] = caption
    if is_carousel_item:
        data["is_carousel_item"] = "true"
    res = ig_post(f"{GRAPH_BASE}/{IG_USER_ID}/media", data, need_requests=1)
    return res.get("id")


def create_carousel_container(children_ids: List[str], caption: str) -> str:
    data = {
        "media_type": "CAROUSEL",
        "children": ",".join(children_ids),
        "caption": caption,
        "access_token": IG_TOKEN,
    }
    res = ig_post(f"{GRAPH_BASE}/{IG_USER_ID}/media", data, need_requests=1)
    return res.get("id")


def publish_container(creation_id: str) -> Dict[str, Any]:
    data = {"creation_id": creation_id, "access_token": IG_TOKEN}
    return ig_post(f"{GRAPH_BASE}/{IG_USER_ID}/media_publish", data, need_requests=1)

# ------------------------ Worker ------------------------

STOP_EVENT = threading.Event()


def safe_caption(item: sqlite3.Row) -> str:
    name = item["name"] or "New Product"
    price = item["price"] or "—"
    link = item["link"] or ""
    cap = CAPTION_TEMPLATE.format(name=name, price=price, link=link)
    # basic trim
    return cap[:2200]


def process_one_post(items: List[sqlite3.Row]) -> bool:
    """Returns True if a post was made, else False."""
    if not items:
        return False

    # pacing guards
    if posts_today() >= DAILY_POST_LIMIT:
        return False
    if minutes_since_last_post() < MIN_MINUTES_BETWEEN_POSTS:
        return False

    # Build media
    if USE_CAROUSEL and len(items) > 1:
        # up to CAROUSEL_MAX_ITEMS images (one per product, first image of each)
        children = []
        used_ids = []
        for r in items[:CAROUSEL_MAX_ITEMS]:
            images = json.loads(r["images_json"]) if r["images_json"] else []
            if not images:
                continue
            try:
                child_id = create_photo_container(images[0], is_carousel_item=True)
                children.append(child_id)
                used_ids.append(r["id"])
                time.sleep(1)  # tiny gap
            except Exception as e:
                mark_items([r["id"]], "error", error=str(e))
        if not children:
            return False
        try:
            caption = safe_caption(items[0])  # leading item caption
            parent_id = create_carousel_container(children, caption)
            publish_container(parent_id)
            incr_posts()
            mark_items(used_ids, "posted")
            return True
        except Exception as e:
            mark_items(used_ids, "error", error=str(e))
            return False
    else:
        # single photo from first queued product
        r = items[0]
        images = json.loads(r["images_json"]) if r["images_json"] else []
        if not images:
            mark_items([r["id"]], "skipped", error="no image")
            return False
        try:
            creation_id = create_photo_container(images[0], caption=safe_caption(r))
            publish_container(creation_id)
            incr_posts()
            mark_items([r["id"]], "posted")
            return True
        except Exception as e:
            mark_items([r["id"]], "error", error=str(e))
            return False


def worker_loop():
    while not STOP_EVENT.is_set():
        try:
            # midnight reset happens implicitly by date key; counters roll to a new day automatically
            # If daily limit hit, sleep until next day
            if posts_today() >= DAILY_POST_LIMIT:
                # sleep until local midnight + small buffer
                now = now_local()
                tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
                time_to_sleep = max(5, int((tomorrow - now).total_seconds()))
                time.sleep(min(time_to_sleep, 300))
                continue

            # If we must wait between posts
            if minutes_since_last_post() < MIN_MINUTES_BETWEEN_POSTS:
                time.sleep(30)
                continue

            # Fetch next batch
            batch_size = CAROUSEL_MAX_ITEMS if USE_CAROUSEL else 1
            items = fetch_next_batch(batch_size)
            if not items:
                time.sleep(20)
                continue

            made = process_one_post(items)
            if not made:
                time.sleep(20)
            else:
                # small idle to avoid hammering
                time.sleep(5)

        except Exception:
            traceback.print_exc()
            time.sleep(30)

# ------------------------ Flask Endpoints ------------------------

@app.get("/health")
def health():
    return {"ok": True, "posts_today": posts_today(), "requests_last_hour": requests_last_hour()}


@app.get("/metrics")
def metrics():
    with _db_lock:
        conn = db()
        try:
            cur = conn.execute("SELECT * FROM metrics ORDER BY d DESC LIMIT 7")
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    return jsonify({
        "config": {
            "DAILY_POST_LIMIT": DAILY_POST_LIMIT,
            "MIN_MINUTES_BETWEEN_POSTS": MIN_MINUTES_BETWEEN_POSTS,
            "USE_CAROUSEL": USE_CAROUSEL,
            "CAROUSEL_MAX_ITEMS": CAROUSEL_MAX_ITEMS,
            "REQUESTS_PER_HOUR_LIMIT": REQUESTS_PER_HOUR_LIMIT,
            "REQUESTS_SAFETY_RATIO": REQUESTS_SAFETY_RATIO,
            "TIMEZONE": TIMEZONE,
        },
        "metrics": rows,
        "queue_size": queue_size(),
    })


def queue_size() -> int:
    with _db_lock:
        conn = db()
        try:
            cur = conn.execute("SELECT COUNT(*) FROM queue WHERE status='queued'")
            return int(cur.fetchone()[0])
        finally:
            conn.close()


@app.post("/wc-webhook")
def wc_webhook():
    """WooCommerce Webhook: Topic = Product created | Format = JSON
    Expected keys (WC typically sends): name, price/regular_price, permalink, images[{src}]
    """
    data = request.get_json(force=True, silent=True) or {}

    # Try common WooCommerce payload shapes
    name = data.get("name") or data.get("title") or "New Product"
    price = data.get("price") or data.get("regular_price") or ""
    link = data.get("permalink") or data.get("url") or ""
    images = []

    # images often under images: [{src:"..."}]
    imgs = data.get("images") or []
    if isinstance(imgs, list):
        for it in imgs:
            if isinstance(it, dict) and it.get("src"):
                images.append(it["src"]) 

    # Fallback: featured_image or first gallery url
    if not images and isinstance(data.get("featured_image"), str):
        images.append(data["featured_image"]) 

    if not images:
        # accept but mark skipped later during processing
        pass

    enqueue_item(name=name, price=str(price), link=link, images=images)
    return jsonify({"status": "queued", "name": name, "images": len(images)})


# ------------------------ Main ------------------------

from flask import Flask, request

app = Flask(__name__)

# Test Route
@app.route("/", methods=["GET"])
def home():
    return "Server is running successfully!"

# Webhook Verification Route
@app.route("/webhook", methods=["GET"])
def verify():
    verify_token = "my_verify_token"   # apna verify token yaha daalo
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == verify_token:
        return challenge, 200
    else:
        return "Verification failed", 403


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)


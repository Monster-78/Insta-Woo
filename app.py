#!/usr/bin/env python3
import os
import json
import time
import threading
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import requests

# ------------------------ Config ------------------------
def env_bool(key, default=True):
    val = os.getenv(key)
    if val is None:
        return default
    return str(val).strip().lower() in {"1","true","yes","y","on"}

PORT = int(os.getenv("PORT", "8000"))
IG_USER_ID = os.getenv("IG_USER_ID","")  # Instagram Business ID
IG_TOKEN = os.getenv("IG_TOKEN","")      # Long-lived Instagram token
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN","my_verify_token")
DAILY_POST_LIMIT = int(os.getenv("DAILY_POST_LIMIT","10"))
MIN_MINUTES_BETWEEN_POSTS = int(os.getenv("MIN_MINUTES_BETWEEN_POSTS","30"))
USE_CAROUSEL = env_bool("USE_CAROUSEL", True)
CAROUSEL_MAX_ITEMS = max(1,min(10,int(os.getenv("CAROUSEL_MAX_ITEMS","10"))))
DB_PATH = os.getenv("DB_PATH","data.sqlite3")
GRAPH_BASE = "https://graph.facebook.com/v21.0"

# ------------------------ Flask / DB ------------------------
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
    status TEXT DEFAULT 'queued',
    error TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    posted_at TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    d TEXT PRIMARY KEY,
    posts_made INTEGER DEFAULT 0,
    last_post_at TEXT
);
"""

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _db_lock:
        conn = db()
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

# ------------------------ Queue ------------------------
def enqueue_item(name, price, link, images):
    with _db_lock:
        conn = db()
        conn.execute(
            "INSERT INTO queue(name,price,link,images_json) VALUES (?,?,?,?)",
            (name, price, link, json.dumps(images))
        )
        conn.commit()
        conn.close()

def fetch_next_batch(max_items=1):
    with _db_lock:
        conn = db()
        cur = conn.execute("SELECT * FROM queue WHERE status='queued' ORDER BY id ASC LIMIT ?", (max_items,))
        rows = cur.fetchall()
        conn.close()
        return rows

def mark_items(ids, status, error=None):
    if not ids:
        return
    with _db_lock:
        conn = db()
        for _id in ids:
            conn.execute(
                "UPDATE queue SET status=?, error=?, posted_at=datetime('now') WHERE id=?",
                (status, error, _id)
            )
        conn.commit()
        conn.close()

# ------------------------ Instagram Posting ------------------------
class IGError(Exception): pass

def ig_post(url, data):
    try:
        resp = requests.post(url, data=data, timeout=30)
    except Exception as e:
        raise IGError(f"network error: {e}")
    if resp.status_code >= 400:
        raise IGError(resp.text)
    return resp.json()

def publish_item(item):
    images = json.loads(item["images_json"]) if item["images_json"] else []
    if not images:
        mark_items([item["id"]],"skipped","no image")
        return
    try:
        creation = ig_post(f"{GRAPH_BASE}/{IG_USER_ID}/media",
                           {"image_url": images[0], "caption": item["name"], "access_token": IG_TOKEN})
        ig_post(f"{GRAPH_BASE}/{IG_USER_ID}/media_publish",
                {"creation_id": creation["id"], "access_token": IG_TOKEN})
        mark_items([item["id"]], "posted")
    except Exception as e:
        mark_items([item["id"]], "error", str(e))

# ------------------------ Worker ------------------------
def posts_today():
    with _db_lock:
        conn = db()
        cur = conn.execute("SELECT posts_made FROM metrics WHERE d=?", (datetime.now().strftime("%Y-%m-%d"),))
        row = cur.fetchone()
        conn.close()
        return int(row[0]) if row and row[0] else 0

def minutes_since_last_post():
    with _db_lock:
        conn = db()
        cur = conn.execute("SELECT last_post_at FROM metrics WHERE d=?", (datetime.now().strftime("%Y-%m-%d"),))
        row = cur.fetchone()
        conn.close()
        if not row or not row[0]:
            return 1e9
        last = datetime.fromisoformat(row[0])
        delta = datetime.now() - last
        return max(0, delta.total_seconds()/60)

def incr_posts():
    with _db_lock:
        conn = db()
        d = datetime.now().strftime("%Y-%m-%d")
        conn.execute("INSERT INTO metrics(d) VALUES (?) ON CONFLICT(d) DO NOTHING", (d,))
        conn.execute("UPDATE metrics SET posts_made = COALESCE(posts_made,0)+1, last_post_at=? WHERE d=?",
                     (datetime.now().isoformat(), d))
        conn.commit()
        conn.close()

def worker_loop():
    while True:
        if posts_today() >= DAILY_POST_LIMIT or minutes_since_last_post() < MIN_MINUTES_BETWEEN_POSTS:
            time.sleep(30)
            continue
        batch = fetch_next_batch(CAROUSEL_MAX_ITEMS if USE_CAROUSEL else 1)
        if batch:
            for item in batch:
                publish_item(item)
                incr_posts()
        else:
            time.sleep(20)

# ------------------------ Flask Endpoints ------------------------
@app.route("/")
def home():
    return "Server is running successfully!"

@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode=="subscribe" and token==VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403

    
@app.route("/wc-webhook", methods=["POST"])
def wc_webhook():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name") or "New Product"
    price = data.get("price") or ""
    link = data.get("permalink") or ""
    images = []
    imgs = data.get("images") or []
    if isinstance(imgs, list):
        for it in imgs:
            if isinstance(it, dict) and it.get("src"):
                images.append(it["src"])
    enqueue_item(name, price, link, images)
    # Instant response
    return jsonify({"status": "queued"}), 200

# ------------------------ Main ------------------------
if __name__=="__main__":
    init_db()
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=PORT)

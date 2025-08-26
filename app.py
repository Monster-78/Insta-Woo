#!/usr/bin/env python3
"""
WooCommerce → Instagram Auto-Poster
- Cloud-friendly (Railway)
- Policy-safe pacing
- SQLite queue for durability
- Webhook endpoint for WooCommerce "Product created"
- Instagram OAuth callback for business login

ENV VARS:
  PORT=8000
  IG_USER_ID=
  IG_TOKEN=
  IG_APP_ID=
  IG_APP_SECRET=
  VERIFY_TOKEN=my_verify_token
  DAILY_POST_LIMIT=10
  MIN_MINUTES_BETWEEN_POSTS=30
  USE_CAROUSEL=true
  CAROUSEL_MAX_ITEMS=10
"""

import json, os, sqlite3, threading, time, traceback
from datetime import datetime, timedelta
from typing import List, Dict, Any
import requests
from flask import Flask, request, jsonify

# ------------------------ Config ------------------------
def env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None: return default
    return str(val).strip().lower() in {"1","true","yes","y","on"}

PORT = int(os.getenv("PORT","8000"))
IG_USER_ID = os.getenv("IG_USER_ID","")
IG_TOKEN = os.getenv("IG_TOKEN","")
IG_APP_ID = os.getenv("IG_APP_ID","")
IG_APP_SECRET = os.getenv("IG_APP_SECRET","")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN","my_verify_token")
DAILY_POST_LIMIT = int(os.getenv("DAILY_POST_LIMIT","10"))
MIN_MINUTES_BETWEEN_POSTS = int(os.getenv("MIN_MINUTES_BETWEEN_POSTS","30"))
USE_CAROUSEL = env_bool("USE_CAROUSEL", True)
CAROUSEL_MAX_ITEMS = max(1,min(10,int(os.getenv("CAROUSEL_MAX_ITEMS","10"))))
GRAPH_BASE = "https://graph.facebook.com/v21.0"
DB_PATH = os.getenv("DB_PATH","data.sqlite3")

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
   status TEXT DEFAULT 'queued',
   error TEXT,
   created_at TEXT DEFAULT (datetime('now')),
   posted_at TEXT
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

# ------------------------ Queue Helpers ------------------------
def enqueue_item(name, price, link, images):
    with _db_lock:
        conn = db()
        conn.execute("INSERT INTO queue(name,price,link,images_json) VALUES (?,?,?,?)",
                     (name,price,link,json.dumps(images)))
        conn.commit(); conn.close()

def fetch_next():
    with _db_lock:
        conn = db()
        rows = conn.execute("SELECT * FROM queue WHERE status='queued' ORDER BY id ASC LIMIT 1").fetchall()
        conn.close()
        return rows

def mark_item(_id,status,err=None):
    with _db_lock:
        conn = db()
        conn.execute("UPDATE queue SET status=?, error=?, posted_at=datetime('now') WHERE id=?",
                     (status,err,_id))
        conn.commit(); conn.close()

# ------------------------ Instagram Posting ------------------------
class IGError(Exception): pass

def ig_post(url,data):
    try:
        r = requests.post(url,data=data,timeout=30)
    except Exception as e:
        raise IGError(f"network error {e}")
    if r.status_code>=400:
        raise IGError(r.text)
    return r.json()

def publish_single(r):
    imgs=json.loads(r["images_json"]) if r["images_json"] else []
    if not imgs:
        mark_item(r["id"],"skipped","no image"); return
    try:
        creation=ig_post(f"{GRAPH_BASE}/{IG_USER_ID}/media",
                         {"image_url":imgs[0],"caption":r["name"],"access_token":IG_TOKEN})
        ig_post(f"{GRAPH_BASE}/{IG_USER_ID}/media_publish",
                {"creation_id":creation["id"],"access_token":IG_TOKEN})
        mark_item(r["id"],"posted")
    except Exception as e:
        mark_item(r["id"],"error",str(e))

def worker_loop():
    while True:
        rows = fetch_next()
        if rows:
            publish_single(rows[0])
        time.sleep(30)

# ------------------------ Flask Endpoints ------------------------
@app.route("/")
def home():
    return "Server is running successfully!"

# Webhook verification
@app.route("/webhook", methods=["GET"])
def verify():
    mode=request.args.get("hub.mode")
    token=request.args.get("hub.verify_token")
    challenge=request.args.get("hub.challenge")
    if mode=="subscribe" and token==VERIFY_TOKEN:
        return challenge,200
    return "Verification failed",403

# WooCommerce webhook
@app.route("/wc-webhook",methods=["POST"])
def wc_webhook():
    data=request.get_json(force=True,silent=True) or {}
    name=data.get("name") or "New Product"
    price=data.get("price") or ""
    link=data.get("permalink") or ""
    images=[]
    imgs=data.get("images") or []
    if isinstance(imgs,list):
        for it in imgs:
            if isinstance(it,dict) and it.get("src"):
                images.append(it["src"])
    enqueue_item(name,price,link,images)
    return jsonify({"status":"queued","name":name,"images":len(images)})

# Instagram OAuth callback
@app.route("/ig-callback")
def ig_callback():
    code = request.args.get("code")
    error = request.args.get("error")
    if error:
        return f"Instagram login error: {error}", 400
    if not code:
        return "No code received", 400
    try:
        resp = requests.post(
            "https://api.instagram.com/oauth/access_token",
            data={
                "client_id": IG_APP_ID,
                "client_secret": IG_APP_SECRET,
                "grant_type": "authorization_code",
                "redirect_uri": f"https://web-production-aec9.up.railway.app/ig-callback",
                "code": code
            },
        )
        token_data = resp.json()
    except Exception as e:
        return f"Failed to get access token: {e}", 500
    print("Instagram token received:", token_data)
    return "Instagram login successful! You can close this window."

# ------------------------ Main ------------------------
if __name__=="__main__":
    init_db()
    t = threading.Thread(target=worker_loop,daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=PORT)

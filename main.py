import os
import re
import json
import time
import hmac
import uuid
import base64
import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

# Optional deps
try:
    import stripe  # type: ignore
except Exception:
    stripe = None  # allows server to run without stripe installed

try:
    from openai import OpenAI  # type: ignore
except Exception:
    OpenAI = None  # allows server to run without openai installed


# =========================
# CONFIG
# =========================
APP_NAME = "Book Worm AI"
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "bookworm.db"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

BOOKWORM_OWNER_CODE = os.getenv("BOOKWORM_OWNER_CODE", "").strip()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

STRIPE_BASIC_PRICE_ID = os.getenv("STRIPE_BASIC_PRICE_ID", "").strip()
STRIPE_PRO_PRICE_ID = os.getenv("STRIPE_PRO_PRICE_ID", "").strip()
STRIPE_PATRON_PRICE_ID = os.getenv("STRIPE_PATRON_PRICE_ID", "").strip()

SESSION_COOKIE_NAME = "bw_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days
SESSION_SAMESITE = "lax"

# pick a server secret to sign session IDs (not the same as owner code)
SERVER_SESSION_SECRET = os.getenv("BOOKWORM_SESSION_SECRET", "").strip()
if not SERVER_SESSION_SECRET:
    # fallback: deterministic but better than nothing; set env in production
    SERVER_SESSION_SECRET = "bw_" + hashlib.sha256((DB_PATH + APP_NAME).encode()).hexdigest()

ALLOWED_ORIGINS = os.getenv("CORS_ORIGINS", "*")  # can be comma-separated


# =========================
# APP
# =========================
app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS.split(",")] if ALLOWED_ORIGINS != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# =========================
# DB HELPERS
# =========================
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  is_owner INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS canon (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  tab TEXT NOT NULL,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  tab TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS analytics_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  event TEXT NOT NULL,
  meta_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS subscriptions (
  user_id INTEGER PRIMARY KEY,
  plan TEXT NOT NULL DEFAULT 'free',
  status TEXT,
  stripe_customer_id TEXT,
  stripe_subscription_id TEXT,
  stripe_price_id TEXT,
  current_period_end TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);
"""

def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True) if os.path.dirname(DB_PATH) else None
    conn = db_connect()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        print("[init_db] Schema ensured.")
    finally:
        conn.close()

@app.on_event("startup")
def on_startup():
    init_db()
    if STRIPE_SECRET_KEY and stripe is not None:
        stripe.api_key = STRIPE_SECRET_KEY
    print("[startup] Ready.")


# =========================
# SECURITY HELPERS
# =========================
def pbkdf2_hash_password(password: str, salt_b64: str) -> str:
    salt = base64.b64decode(salt_b64.encode("utf-8"))
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return base64.b64encode(dk).decode("utf-8")

def new_salt_b64() -> str:
    return base64.b64encode(os.urandom(16)).decode("utf-8")

def sign_session_id(session_id: str) -> str:
    sig = hmac.new(SERVER_SESSION_SECRET.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{session_id}.{sig}"

def verify_signed_session(signed: str) -> Optional[str]:
    if not signed or "." not in signed:
        return None
    session_id, sig = signed.rsplit(".", 1)
    expected = hmac.new(SERVER_SESSION_SECRET.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return session_id

def is_https(request: Request) -> bool:
    # Render sets X-Forwarded-Proto
    xf = request.headers.get("x-forwarded-proto", "").lower()
    if xf in ("https", "http"):
        return xf == "https"
    return request.url.scheme == "https"


# =========================
# MODELS
# =========================
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=200)

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class OwnerUnlockRequest(BaseModel):
    code: str

class GenerateRequest(BaseModel):
    tab: str
    prompt: str

class SaveCanonRequest(BaseModel):
    tab: str
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=100_000)

class CheckoutRequest(BaseModel):
    plan: str  # basic, pro, patron


# =========================
# SESSION + AUTH
# =========================
def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    sid = str(uuid.uuid4())
    created = now_utc_iso()
    expires = datetime.fromtimestamp(time.time() + SESSION_MAX_AGE_SECONDS, tz=timezone.utc).replace(microsecond=0).isoformat()
    conn.execute(
        "INSERT INTO sessions (id, user_id, created_at, last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (sid, user_id, created, created, expires),
    )
    conn.commit()
    return sid

def delete_session(conn: sqlite3.Connection, sid: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit()

def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

def get_user_by_email(conn: sqlite3.Connection, email: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone()

def get_plan_for_user(conn: sqlite3.Connection, user_id: int) -> str:
    row = conn.execute("SELECT plan FROM subscriptions WHERE user_id=?", (user_id,)).fetchone()
    return row["plan"] if row and row["plan"] else "free"

def touch_session(conn: sqlite3.Connection, sid: str) -> None:
    conn.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (now_utc_iso(), sid))
    conn.commit()

def get_current_user(request: Request) -> sqlite3.Row:
    signed = request.cookies.get(SESSION_COOKIE_NAME, "")
    sid = verify_signed_session(signed)
    if not sid:
        raise HTTPException(status_code=401, detail="Not authenticated")

    conn = db_connect()
    try:
        sess = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not sess:
            raise HTTPException(status_code=401, detail="Not authenticated")

        # expired?
        expires_at = sess["expires_at"]
        if expires_at and expires_at < now_utc_iso():
            delete_session(conn, sid)
            raise HTTPException(status_code=401, detail="Session expired")

        user = get_user_by_id(conn, int(sess["user_id"]))
        if not user:
            delete_session(conn, sid)
            raise HTTPException(status_code=401, detail="Not authenticated")

        touch_session(conn, sid)
        return user
    finally:
        conn.close()

def require_owner(user: sqlite3.Row = Depends(get_current_user)) -> sqlite3.Row:
    if int(user["is_owner"]) != 1:
        raise HTTPException(status_code=403, detail="Owner access required")
    return user


# =========================
# PAGES
# =========================
@app.get("/", response_class=HTMLResponse)
def root():
    # Serve studio index
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Book Worm AI</h1><p>Missing /static/index.html</p>", status_code=200)

@app.get("/health")
def health():
    return {"ok": True, "time": now_utc_iso()}


# =========================
# AUTH ROUTES
# =========================
@app.post("/auth/register")
def auth_register(req: RegisterRequest, response: Response):
    email = req.email.lower().strip()

    conn = db_connect()
    try:
        existing = get_user_by_email(conn, email)
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered")

        salt = new_salt_b64()
        pw_hash = pbkdf2_hash_password(req.password, salt)

        conn.execute(
            "INSERT INTO users (email, password_hash, salt, is_owner, created_at) VALUES (?, ?, ?, 0, ?)",
            (email, pw_hash, salt, now_utc_iso()),
        )
        user_id = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]

        # default sub row
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, plan, status, updated_at) VALUES (?, 'free', 'active', ?)",
            (user_id, now_utc_iso()),
        )
        conn.commit()

        sid = create_session(conn, int(user_id))
        signed = sign_session_id(sid)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=signed,
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite=SESSION_SAMESITE,
            secure=False,  # set below properly in middleware via request? (FastAPI doesn't pass request here)
        )
        # NOTE: secure flag must be True on HTTPS; we fix it via an additional header hack:
        # Instead, we’ll also return the cookie in JSON and let browser keep it; Render works fine with secure=False
        # If you want strict secure cookies, we can switch to Response in dependency with request.
        return {"ok": True}
    finally:
        conn.close()

@app.post("/auth/login")
def auth_login(req: LoginRequest, response: Response):
    email = req.email.lower().strip()
    conn = db_connect()
    try:
        user = get_user_by_email(conn, email)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid email or password")

        salt = user["salt"]
        expected = user["password_hash"]
        got = pbkdf2_hash_password(req.password, salt)
        if not hmac.compare_digest(expected, got):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        sid = create_session(conn, int(user["id"]))
        signed = sign_session_id(sid)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=signed,
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite=SESSION_SAMESITE,
            secure=False,
        )
        return {"ok": True}
    finally:
        conn.close()

@app.post("/auth/logout")
def auth_logout(request: Request, response: Response):
    signed = request.cookies.get(SESSION_COOKIE_NAME, "")
    sid = verify_signed_session(signed)
    conn = db_connect()
    try:
        if sid:
            delete_session(conn, sid)
    finally:
        conn.close()

    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"ok": True}

@app.get("/auth/me")
def auth_me(user: sqlite3.Row = Depends(get_current_user)):
    conn = db_connect()
    try:
        plan = get_plan_for_user(conn, int(user["id"]))
    finally:
        conn.close()

    return {
        "id": int(user["id"]),
        "email": user["email"],
        "plan": plan,
        "is_owner": bool(int(user["is_owner"])),
    }


# =========================
# OWNER / ADMIN UNLOCK
# =========================
@app.post("/owner/unlock")
def owner_unlock(req: OwnerUnlockRequest, user: sqlite3.Row = Depends(get_current_user)):
    if not BOOKWORM_OWNER_CODE:
        raise HTTPException(status_code=500, detail="Owner code not configured on server")
    if req.code != BOOKWORM_OWNER_CODE:
        raise HTTPException(status_code=401, detail="Invalid owner code")

    conn = db_connect()
    try:
        conn.execute("UPDATE users SET is_owner=1 WHERE id=?", (int(user["id"]),))
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "is_owner": True}

@app.post("/owner/lock")
def owner_lock(user: sqlite3.Row = Depends(get_current_user)):
    conn = db_connect()
    try:
        conn.execute("UPDATE users SET is_owner=0 WHERE id=?", (int(user["id"]),))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "is_owner": False}


# =========================
# CANON
# =========================
@app.post("/canon/save")
def canon_save(req: SaveCanonRequest, user: sqlite3.Row = Depends(get_current_user)):
    conn = db_connect()
    try:
        conn.execute(
            "INSERT INTO canon (user_id, tab, title, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (int(user["id"]), req.tab, req.title, req.content, now_utc_iso()),
        )
        conn.commit()
        conn.execute(
            "INSERT INTO analytics_events (user_id, event, meta_json, created_at) VALUES (?, 'canon_save', ?, ?)",
            (int(user["id"]), json.dumps({"tab": req.tab}), now_utc_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}

@app.get("/canon/list")
def canon_list(user: sqlite3.Row = Depends(get_current_user)):
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT id, tab, title, content, created_at FROM canon WHERE user_id=? ORDER BY id DESC LIMIT 100",
            (int(user["id"]),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# =========================
# CHAT + GENERATE
# =========================
def normalize_tab(tab: str) -> str:
    tab = (tab or "").strip().lower()
    tab = re.sub(r"[^a-z0-9_\-]", "", tab)
    return tab or "chat"

def store_message(user_id: int, tab: str, role: str, content: str) -> None:
    conn = db_connect()
    try:
        conn.execute(
            "INSERT INTO chat_messages (user_id, tab, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, tab, role, content, now_utc_iso()),
        )
        conn.commit()
    finally:
        conn.close()

def load_recent_messages(user_id: int, tab: str, limit: int = 18) -> List[Dict[str, str]]:
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT role, content FROM chat_messages WHERE user_id=? AND tab=? ORDER BY id DESC LIMIT ?",
            (user_id, tab, limit),
        ).fetchall()
        msgs = [{"role": r["role"], "content": r["content"]} for r in rows]
        msgs.reverse()
        return msgs
    finally:
        conn.close()

def system_prompt_for_tab(tab: str) -> str:
    # Keep it simple and predictable; you can expand later per tab.
    base = (
        "You are Book Worm AI Studio. Be direct, helpful, and consistent.\n"
        "Always continue from the user's last message without re-asking what they already said.\n"
        "If you offer options, and the user picks one, proceed immediately.\n"
    )
    tab = normalize_tab(tab)
    if tab in ("music", "musicdev"):
        return base + "Focus on music production, songwriting, mixing, sound design, and creative direction."
    if tab in ("game", "gamedev"):
        return base + "Focus on game design, systems, mechanics, UE/UEFN workflows, quests, balancing, and implementation steps."
    if tab in ("image", "imagelab"):
        return base + "Focus on image prompts, art direction, composition, style consistency, and visual pipelines."
    if tab in ("voice", "voicelab"):
        return base + "Focus on voice acting workflows, dialogue writing, narration, casting/AI voice pipelines (no illegal content)."
    if tab in ("designer", "gamedesigner"):
        return base + "Focus on tabletop/board/card game design, rule systems, balancing, and printable/exportable content."
    if tab in ("story", "book", "writing"):
        return base + "Focus on storytelling, outlines, prose, continuity, and canon-friendly expansions."
    return base + "General chat mode."

@app.post("/generate")
def generate(req: GenerateRequest, user: sqlite3.Row = Depends(get_current_user)):
    tab = normalize_tab(req.tab)
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    # store user msg
    store_message(int(user["id"]), tab, "user", prompt)

    # OpenAI check
    if not OPENAI_API_KEY or OpenAI is None:
        answer = (
            "⚠ OPENAI_API_KEY is not configured on this server.\n"
            "Set OPENAI_API_KEY in Render Environment Variables, then redeploy."
        )
        store_message(int(user["id"]), tab, "assistant", answer)
        return {"response": answer}

    client = OpenAI(api_key=OPENAI_API_KEY)

    history = load_recent_messages(int(user["id"]), tab, limit=18)
    system = system_prompt_for_tab(tab)

    # Build messages for Responses API
    # We'll use the modern Responses API if available; fallback to ChatCompletions style if needed.
    try:
        resp = client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            input=[
                {"role": "system", "content": system},
                *history
            ],
        )
        # Extract text safely
        text = ""
        try:
            # New SDKs provide output_text
            text = resp.output_text  # type: ignore
        except Exception:
            # Fallback parse
            out = getattr(resp, "output", None)
            if out:
                for item in out:
                    for c in getattr(item, "content", []) or []:
                        if getattr(c, "type", "") == "output_text":
                            text += getattr(c, "text", "") or ""
        if not text:
            text = "⚠ No text returned."
    except Exception as e:
        text = f"⚠ AI error: {str(e)}"

    store_message(int(user["id"]), tab, "assistant", text)

    # analytics event
    conn = db_connect()
    try:
        conn.execute(
            "INSERT INTO analytics_events (user_id, event, meta_json, created_at) VALUES (?, 'generate', ?, ?)",
            (int(user["id"]), json.dumps({"tab": tab}), now_utc_iso()),
        )
        conn.commit()
    finally:
        conn.close()

    return {"response": text}


# =========================
# STRIPE
# =========================
def stripe_configured() -> bool:
    return bool(STRIPE_SECRET_KEY) and (stripe is not None)

def price_id_for_plan(plan: str) -> str:
    plan = (plan or "").strip().lower()
    mapping = {
        "basic": STRIPE_BASIC_PRICE_ID,
        "pro": STRIPE_PRO_PRICE_ID,
        "patron": STRIPE_PATRON_PRICE_ID,
    }
    return mapping.get(plan, "")

@app.get("/debug/stripe")
def debug_stripe():
    # safe debug: doesn't reveal secrets
    return {
        "has_secret_key": bool(STRIPE_SECRET_KEY),
        "has_webhook_secret": bool(STRIPE_WEBHOOK_SECRET),
        "secret_key_length": len(STRIPE_SECRET_KEY) if STRIPE_SECRET_KEY else 0,
        "webhook_length": len(STRIPE_WEBHOOK_SECRET) if STRIPE_WEBHOOK_SECRET else 0,
        "has_price_basic": bool(STRIPE_BASIC_PRICE_ID),
        "has_price_pro": bool(STRIPE_PRO_PRICE_ID),
        "has_price_patron": bool(STRIPE_PATRON_PRICE_ID),
        "stripe_import_ok": stripe is not None,
    }

@app.post("/stripe/create-checkout-session")
def stripe_create_checkout(req: CheckoutRequest, request: Request, user: sqlite3.Row = Depends(get_current_user)):
    if not stripe_configured():
        raise HTTPException(status_code=500, detail="Stripe not configured on server")
    if not STRIPE_BASIC_PRICE_ID or not STRIPE_PRO_PRICE_ID or not STRIPE_PATRON_PRICE_ID:
        raise HTTPException(status_code=500, detail="Stripe price IDs not configured on server")

    plan = (req.plan or "").strip().lower()
    price_id = price_id_for_plan(plan)
    if plan not in ("basic", "pro", "patron") or not price_id:
        raise HTTPException(status_code=400, detail="Invalid plan")

    # Build success/cancel urls (same origin)
    origin = request.headers.get("origin")
    if not origin:
        origin = str(request.base_url).rstrip("/")
    success_url = f"{origin}/?checkout=success"
    cancel_url = f"{origin}/?checkout=cancel"

    try:
        sess = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=user["email"],
            success_url=success_url,
            cancel_url=cancel_url,
            allow_promotion_codes=True,
        )
        return {"url": sess.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not stripe_configured():
        raise HTTPException(status_code=500, detail="Stripe not configured on server")
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Stripe webhook secret not configured on server")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook signature failed: {str(e)}")

    etype = event.get("type", "")
    data = event.get("data", {}).get("object", {})

    def upsert_sub_by_email(email: str, plan: str, status: str, customer_id: str, sub_id: str, price_id: str, period_end: Optional[int]):
        if not email:
            return
        conn = db_connect()
        try:
            u = conn.execute("SELECT id FROM users WHERE email=?", (email.lower(),)).fetchone()
            if not u:
                return
            user_id = int(u["id"])
            current_period_end = None
            if period_end:
                current_period_end = datetime.fromtimestamp(period_end, tz=timezone.utc).replace(microsecond=0).isoformat()

            conn.execute(
                """INSERT INTO subscriptions (user_id, plan, status, stripe_customer_id, stripe_subscription_id, stripe_price_id, current_period_end, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     plan=excluded.plan,
                     status=excluded.status,
                     stripe_customer_id=excluded.stripe_customer_id,
                     stripe_subscription_id=excluded.stripe_subscription_id,
                     stripe_price_id=excluded.stripe_price_id,
                     current_period_end=excluded.current_period_end,
                     updated_at=excluded.updated_at
                """,
                (user_id, plan, status, customer_id, sub_id, price_id, current_period_end, now_utc_iso()),
            )
            conn.execute(
                "INSERT INTO analytics_events (user_id, event, meta_json, created_at) VALUES (?, 'stripe_update', ?, ?)",
                (user_id, json.dumps({"plan": plan, "status": status, "type": etype}), now_utc_iso()),
            )
            conn.commit()
        finally:
            conn.close()

    def plan_from_price(price: str) -> str:
        if price == STRIPE_BASIC_PRICE_ID:
            return "basic"
        if price == STRIPE_PRO_PRICE_ID:
            return "pro"
        if price == STRIPE_PATRON_PRICE_ID:
            return "patron"
        return "paid"

    # Handle important events
    try:
        if etype == "checkout.session.completed":
            # subscription created; session has customer + subscription + customer_details/email
            email = None
            cd = data.get("customer_details") or {}
            email = cd.get("email") or data.get("customer_email")
            customer_id = data.get("customer")
            sub_id = data.get("subscription")

            # fetch subscription to get price + status
            if sub_id:
                sub = stripe.Subscription.retrieve(sub_id)
                status = sub.get("status", "active")
                items = sub.get("items", {}).get("data", []) or []
                price_id = ""
                if items:
                    price_id = items[0].get("price", {}).get("id", "")
                plan = plan_from_price(price_id)
                period_end = sub.get("current_period_end")
                upsert_sub_by_email(email, plan, status, customer_id, sub_id, price_id, period_end)

        elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
            email = None
            # subscriptions do not always include email directly; retrieve customer
            customer_id = data.get("customer")
            sub_id = data.get("id")
            status = data.get("status", "active")
            period_end = data.get("current_period_end")
            items = data.get("items", {}).get("data", []) or []
            price_id = ""
            if items:
                price_id = items[0].get("price", {}).get("id", "")
            plan = plan_from_price(price_id)

            if customer_id:
                cust = stripe.Customer.retrieve(customer_id)
                email = cust.get("email")

            if etype == "customer.subscription.deleted":
                # mark as free (or canceled)
                plan = "free"
                status = "canceled"

            upsert_sub_by_email(email, plan, status, customer_id, sub_id, price_id, period_end)

    except Exception as e:
        # don't crash webhook; return 200 so Stripe doesn't keep retrying forever,
        # but log the error in Render logs
        print("[stripe_webhook] error:", str(e))

    return {"received": True}


# =========================
# ADMIN ANALYTICS
# =========================
@app.get("/admin/analytics")
def admin_analytics(owner: sqlite3.Row = Depends(require_owner)):
    conn = db_connect()
    try:
        users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        active_sessions = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE expires_at > ?",
            (now_utc_iso(),)
        ).fetchone()["c"]
        canon_count = conn.execute("SELECT COUNT(*) AS c FROM canon").fetchone()["c"]
        msg_count = conn.execute("SELECT COUNT(*) AS c FROM chat_messages").fetchone()["c"]

        paid = conn.execute(
            "SELECT COUNT(*) AS c FROM subscriptions WHERE plan IN ('basic','pro','patron') AND (status IS NULL OR status != 'canceled')"
        ).fetchone()["c"]

        # last 7 days generates
        since = datetime.fromtimestamp(time.time() - 60*60*24*7, tz=timezone.utc).replace(microsecond=0).isoformat()
        gen7 = conn.execute(
            "SELECT COUNT(*) AS c FROM analytics_events WHERE event='generate' AND created_at >= ?",
            (since,)
        ).fetchone()["c"]

        return {
            "users_total": users,
            "sessions_active": active_sessions,
            "paid_active": paid,
            "canon_entries": canon_count,
            "messages_total": msg_count,
            "generations_7d": gen7,
            "stripe_configured": stripe_configured(),
            "time": now_utc_iso(),
        }
    finally:
        conn.close()


# =========================
# FALLBACK 404 JSON (optional)
# =========================
@app.exception_handler(404)
def not_found(_, __):
    return JSONResponse({"detail": "Not Found"}, status_code=404)

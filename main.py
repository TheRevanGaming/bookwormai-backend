import os
import json
import time
import hmac
import base64
import sqlite3
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Any, Dict, List

from fastapi import (
    FastAPI,
    HTTPException,
    Depends,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from pydantic import BaseModel, EmailStr

# OpenAI (optional but expected)
try:
    from openai import OpenAI  # type: ignore
except Exception:
    OpenAI = None  # type: ignore

# Stripe (optional; required if monetizing)
try:
    import stripe  # type: ignore
except Exception:
    stripe = None  # type: ignore


# =========================
# CONFIG
# =========================
APP_NAME = os.getenv("APP_NAME", "Book Worm AI")
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "bookworm.db"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

BOOKWORM_OWNER_CODE = os.getenv("BOOKWORM_OWNER_CODE", "")

# Cookie config
COOKIE_NAME = os.getenv("BOOKWORM_SESSION_COOKIE", "bookworm_session")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "auto")  # "true"/"false"/"auto"
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "lax")  # "lax"/"strict"/"none"
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", "")  # optional
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))

# CORS
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

# Frontend URL used by Stripe redirects (your Studio site)
FRONTEND_URL = os.getenv("FRONTEND_URL", "")  # optional; if blank we fallback to same origin

# Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_BASIC_PRICE_ID = os.getenv("STRIPE_BASIC_PRICE_ID", "")
STRIPE_PRO_PRICE_ID = os.getenv("STRIPE_PRO_PRICE_ID", "")
STRIPE_PATRON_PRICE_ID = os.getenv("STRIPE_PATRON_PRICE_ID", "")

if stripe is not None and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# =========================
# APP
# =========================
app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# =========================
# DB UTIL
# =========================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_utc_iso() -> str:
    return now_utc().isoformat()

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def apply_schema_sql_file(conn: sqlite3.Connection) -> None:
    schema_path = os.path.join(os.path.dirname(__file__), "db", "schema.sql")
    if os.path.exists(schema_path):
        with open(schema_path, "r", encoding="utf-8") as f:
            sql = f.read()
        if sql.strip():
            conn.executescript(sql)

def apply_min_schema(conn: sqlite3.Connection) -> None:
    # Minimal schema to guarantee login/messages/canon/analytics/subscriptions work even if schema.sql changes.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            pass_hash TEXT NOT NULL,
            pass_salt TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_owner INTEGER NOT NULL DEFAULT 0,
            stripe_customer_id TEXT DEFAULT '',
            plan TEXT DEFAULT 'free',
            subscription_status TEXT DEFAULT 'none'
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            is_owner INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, name),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tab TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS canon_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            project_id INTEGER DEFAULT NULL,
            tab TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS analytics_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            meta_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            stripe_customer_id TEXT NOT NULL,
            stripe_subscription_id TEXT NOT NULL,
            status TEXT NOT NULL,
            plan TEXT NOT NULL,
            current_period_end TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )

def init_db() -> None:
    conn = db_connect()
    try:
        # Try schema.sql first, but NEVER let it brick startup
        try:
            apply_schema_sql_file(conn)
        except Exception as e:
            print(f"[init_db] Error applying schema.sql: {e}")

        # Always ensure minimum tables exist
        apply_min_schema(conn)
        conn.commit()
        print("[init_db] DB ready.")
    finally:
        conn.close()


@app.on_event("startup")
def on_startup():
    init_db()


# =========================
# AUTH UTILS
# =========================
def pbkdf2_hash(password: str, salt_b64: str) -> str:
    salt = base64.b64decode(salt_b64.encode("utf-8"))
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return base64.b64encode(dk).decode("utf-8")

def new_salt_b64() -> str:
    return base64.b64encode(secrets.token_bytes(16)).decode("utf-8")

def make_session_token() -> str:
    return secrets.token_urlsafe(32)

def cookie_secure_flag(request: Request) -> bool:
    # "auto": secure if https
    if COOKIE_SECURE.lower() == "true":
        return True
    if COOKIE_SECURE.lower() == "false":
        return False
    return request.url.scheme == "https"

def set_session_cookie(response: Response, request: Request, token: str) -> None:
    kwargs = {
        "key": COOKIE_NAME,
        "value": token,
        "httponly": True,
        "secure": cookie_secure_flag(request),
        "samesite": COOKIE_SAMESITE,
        "path": "/",
    }
    if COOKIE_DOMAIN.strip():
        kwargs["domain"] = COOKIE_DOMAIN.strip()
    response.set_cookie(**kwargs)

def clear_session_cookie(response: Response) -> None:
    kwargs = {
        "key": COOKIE_NAME,
        "value": "",
        "httponly": True,
        "secure": False,
        "samesite": COOKIE_SAMESITE,
        "path": "/",
        "max_age": 0,
        "expires": 0,
    }
    if COOKIE_DOMAIN.strip():
        kwargs["domain"] = COOKIE_DOMAIN.strip()
    response.set_cookie(**kwargs)

def get_session_token_from_request(request: Request) -> str:
    return request.cookies.get(COOKIE_NAME, "") or ""


def get_current_user(request: Request) -> sqlite3.Row:
    token = get_session_token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in")

    conn = db_connect()
    try:
        sess = conn.execute(
            "SELECT * FROM sessions WHERE token = ?",
            (token,),
        ).fetchone()
        if not sess:
            raise HTTPException(status_code=401, detail="Session expired. Please log in again.")

        # Expiry check
        expires_at = datetime.fromisoformat(sess["expires_at"])
        if expires_at < now_utc():
            conn.execute("DELETE FROM sessions WHERE id = ?", (sess["id"],))
            conn.commit()
            raise HTTPException(status_code=401, detail="Session expired. Please log in again.")

        user = conn.execute("SELECT * FROM users WHERE id = ?", (sess["user_id"],)).fetchone()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")

        # attach session owner flag as synthetic column by copying into dict-ish row usage
        # (we just re-fetch in helpers when needed)
        return user
    finally:
        conn.close()


def is_owner_session(request: Request) -> bool:
    token = get_session_token_from_request(request)
    if not token:
        return False
    conn = db_connect()
    try:
        sess = conn.execute("SELECT is_owner FROM sessions WHERE token = ?", (token,)).fetchone()
        return bool(sess and int(sess["is_owner"]) == 1)
    finally:
        conn.close()


# =========================
# SUBSCRIPTION / ACCESS
# =========================
def stripe_configured() -> bool:
    return bool(STRIPE_SECRET_KEY) and (stripe is not None)

def price_id_for_plan(plan: str) -> str:
    plan = (plan or "").strip().lower()
    if plan == "basic":
        return STRIPE_BASIC_PRICE_ID
    if plan == "pro":
        return STRIPE_PRO_PRICE_ID
    if plan == "patron":
        return STRIPE_PATRON_PRICE_ID
    return ""

def user_in_free_trial(user: sqlite3.Row) -> bool:
    try:
        created = datetime.fromisoformat(user["created_at"])
    except Exception:
        return True
    return (created + timedelta(days=30)) > now_utc()

def user_has_active_subscription(user_id: int) -> bool:
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT status FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not row:
            return False
        return (row["status"] or "").lower() in ("active", "trialing")
    finally:
        conn.close()

def require_access(request: Request, user: sqlite3.Row) -> None:
    # Owner always allowed
    if is_owner_session(request) or int(user.get("is_owner", 0) or 0) == 1:
        return
    # Trial allowed
    if user_in_free_trial(user):
        return
    # Paid subscription required
    if user_has_active_subscription(int(user["id"])):
        return
    raise HTTPException(status_code=402, detail="Subscription required. Please choose a plan to continue.")


# =========================
# TAB / HISTORY
# =========================
def normalize_tab(tab: str) -> str:
    t = (tab or "").strip().lower()
    mapping = {
        "chat": "chat",
        "story": "writing",
        "book": "writing",
        "writing": "writing",
        "game": "gamedev",
        "gamedev": "gamedev",
        "music": "musicdev",
        "musicdev": "musicdev",
        "image": "imagelab",
        "imagelab": "imagelab",
        "voice": "voicelab",
        "voicelab": "voicelab",
        "designer": "gamedesigner",
        "gamedesigner": "gamedesigner",
    }
    return mapping.get(t, t if t else "chat")

def store_message(user_id: int, tab: str, role: str, content: str) -> None:
    conn = db_connect()
    try:
        conn.execute(
            "INSERT INTO messages (user_id, tab, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, normalize_tab(tab), role, content, now_utc_iso()),
        )
        conn.commit()
    finally:
        conn.close()

def load_recent_messages(user_id: int, tab: str, limit: int = 18) -> List[Dict[str, str]]:
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE user_id = ? AND tab = ? ORDER BY id DESC LIMIT ?",
            (user_id, normalize_tab(tab), limit),
        ).fetchall()
        rows = list(reversed(rows))
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    finally:
        conn.close()

def system_prompt_for_tab(tab: str) -> str:
    base = (
        "You are Book Worm AI Studio. Be direct, helpful, and consistent.\n"
        "Always continue from the user's last message without re-asking what they already said.\n"
        "If you offer options, and the user picks one, proceed immediately.\n"
    )
    tab = normalize_tab(tab)
    if tab == "musicdev":
        return base + "Focus on music production, songwriting, mixing, sound design, and creative direction."
    if tab == "gamedev":
        return base + "Focus on game design, systems, mechanics, UE/UEFN workflows, quests, balancing, and implementation steps."
    if tab == "imagelab":
        return base + "Focus on image prompts, art direction, composition, style consistency, and visual pipelines."
    if tab == "voicelab":
        return base + "Focus on voice acting workflows, dialogue writing, narration, casting/AI voice pipelines (no illegal content)."
    if tab == "gamedesigner":
        return base + "Focus on tabletop/board/card game design (DnD-like), TCGs (MTG/Pokémon/Yu-Gi-Oh-like), balancing, and export-ready rules/content."
    if tab == "writing":
        return base + "Focus on storytelling, outlines, prose, continuity, and canon-friendly expansions."
    return base + "General chat mode."


# =========================
# MODELS
# =========================
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class OwnerLoginRequest(BaseModel):
    code: str

class GenerateRequest(BaseModel):
    tab: str = "chat"
    prompt: str

class SaveCanonRequest(BaseModel):
    tab: str = "chat"
    title: str
    content: str
    project_id: Optional[int] = None


# =========================
# ROUTES: UI
# =========================
@app.get("/", response_class=HTMLResponse)
def root():
    # Serve studio UI if present
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Book Worm AI backend is running.</h1><p>Add /static/index.html</p>")

@app.get("/health")
def health():
    return {"ok": True, "app": APP_NAME, "time": now_utc_iso()}


# =========================
# ROUTES: AUTH
# =========================
@app.post("/auth/register")
def auth_register(req: RegisterRequest, request: Request):
    email = req.email.strip().lower()
    password = (req.password or "").strip()
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    salt = new_salt_b64()
    pwh = pbkdf2_hash(password, salt)

    conn = db_connect()
    try:
        try:
            conn.execute(
                "INSERT INTO users (email, pass_hash, pass_salt, created_at) VALUES (?, ?, ?, ?)",
                (email, pwh, salt, now_utc_iso()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Account already exists. Please log in.")

        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            raise HTTPException(status_code=500, detail="Registration failed")

        token = make_session_token()
        expires = now_utc() + timedelta(days=SESSION_TTL_DAYS)
        conn.execute(
            "INSERT INTO sessions (user_id, token, created_at, expires_at, is_owner) VALUES (?, ?, ?, ?, 0)",
            (int(user["id"]), token, now_utc_iso(), expires.isoformat()),
        )
        conn.commit()

        resp = JSONResponse({"ok": True, "email": email})
        set_session_cookie(resp, request, token)
        return resp
    finally:
        conn.close()

@app.post("/auth/login")
def auth_login(req: LoginRequest, request: Request):
    email = req.email.strip().lower()
    password = (req.password or "").strip()

    conn = db_connect()
    try:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            raise HTTPException(status_code=401, detail="Invalid email or password")

        expected = pbkdf2_hash(password, user["pass_salt"])
        if not hmac.compare_digest(expected, user["pass_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        token = make_session_token()
        expires = now_utc() + timedelta(days=SESSION_TTL_DAYS)
        conn.execute(
            "INSERT INTO sessions (user_id, token, created_at, expires_at, is_owner) VALUES (?, ?, ?, ?, 0)",
            (int(user["id"]), token, now_utc_iso(), expires.isoformat()),
        )
        conn.commit()

        resp = JSONResponse({"ok": True, "email": email})
        set_session_cookie(resp, request, token)
        return resp
    finally:
        conn.close()

@app.post("/auth/logout")
def auth_logout(request: Request):
    token = get_session_token_from_request(request)
    if token:
        conn = db_connect()
        try:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()
    resp = JSONResponse({"ok": True})
    clear_session_cookie(resp)
    return resp

@app.get("/auth/me")
def auth_me(request: Request):
    token = get_session_token_from_request(request)
    if not token:
        return {"logged_in": False}

    conn = db_connect()
    try:
        sess = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
        if not sess:
            return {"logged_in": False}

        expires_at = datetime.fromisoformat(sess["expires_at"])
        if expires_at < now_utc():
            conn.execute("DELETE FROM sessions WHERE id = ?", (sess["id"],))
            conn.commit()
            return {"logged_in": False}

        user = conn.execute("SELECT * FROM users WHERE id = ?", (sess["user_id"],)).fetchone()
        if not user:
            return {"logged_in": False}

        return {
            "logged_in": True,
            "email": user["email"],
            "is_owner": bool(sess["is_owner"]),
            "trial_active": user_in_free_trial(user),
            "subscription_active": user_has_active_subscription(int(user["id"])),
        }
    finally:
        conn.close()

@app.post("/auth/owner-login")
def owner_login(req: OwnerLoginRequest, request: Request):
    if not BOOKWORM_OWNER_CODE:
        raise HTTPException(status_code=500, detail="Owner code not configured on server.")
    if not hmac.compare_digest((req.code or ""), BOOKWORM_OWNER_CODE):
        raise HTTPException(status_code=401, detail="Invalid owner code")

    # Must be logged in as a user first
    user = get_current_user(request)

    token = get_session_token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in")

    conn = db_connect()
    try:
        conn.execute("UPDATE sessions SET is_owner = 1 WHERE token = ?", (token,))
        conn.execute("UPDATE users SET is_owner = 1 WHERE id = ?", (int(user["id"]),))
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "owner": True}


# =========================
# ROUTES: SETTINGS / ADMIN INFO
# =========================
@app.get("/api/settings")
def api_settings(request: Request):
    # not requiring login for baseline UI boot, but include user info if logged in
    me = {"logged_in": False}
    try:
        me = auth_me(request)
    except Exception:
        pass

    return {
        "app": APP_NAME,
        "studio_url": str(request.base_url).rstrip("/"),
        "tabs": ["chat", "writing", "gamedev", "musicdev", "imagelab", "voicelab", "gamedesigner"],
        "stripe_ready": stripe_configured() and bool(STRIPE_BASIC_PRICE_ID or STRIPE_PRO_PRICE_ID or STRIPE_PATRON_PRICE_ID),
        "me": me,
    }

@app.get("/admin/analytics")
def admin_analytics(request: Request, user: sqlite3.Row = Depends(get_current_user)):
    if not is_owner_session(request):
        raise HTTPException(status_code=403, detail="Owner only")

    conn = db_connect()
    try:
        users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        events = conn.execute("SELECT COUNT(*) AS c FROM analytics_events").fetchone()["c"]
        subs = conn.execute("SELECT COUNT(*) AS c FROM subscriptions WHERE status IN ('active','trialing')").fetchone()["c"]

        by_event = conn.execute(
            "SELECT event, COUNT(*) AS c FROM analytics_events GROUP BY event ORDER BY c DESC LIMIT 20"
        ).fetchall()

        return {
            "users": users,
            "events": events,
            "active_subscriptions": subs,
            "top_events": [{"event": r["event"], "count": r["c"]} for r in by_event],
        }
    finally:
        conn.close()


# =========================
# ROUTES: CANON
# =========================
@app.post("/canon/save")
def save_to_canon(req: SaveCanonRequest, request: Request, user: sqlite3.Row = Depends(get_current_user)):
    require_access(request, user)

    title = (req.title or "").strip()
    content = (req.content or "").strip()
    tab = normalize_tab(req.tab)

    if not title or not content:
        raise HTTPException(status_code=400, detail="Title and content are required")

    conn = db_connect()
    try:
        conn.execute(
            "INSERT INTO canon_entries (user_id, project_id, tab, title, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (int(user["id"]), req.project_id, tab, title, content, now_utc_iso()),
        )
        conn.execute(
            "INSERT INTO analytics_events (user_id, event, meta_json, created_at) VALUES (?, 'canon_save', ?, ?)",
            (int(user["id"]), json.dumps({"tab": tab}), now_utc_iso()),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()

@app.get("/canon/list")
def list_canon(request: Request, tab: str = "", project_id: Optional[int] = None, user: sqlite3.Row = Depends(get_current_user)):
    require_access(request, user)

    tab_norm = normalize_tab(tab) if tab else ""
    conn = db_connect()
    try:
        if tab_norm and project_id is not None:
            rows = conn.execute(
                "SELECT * FROM canon_entries WHERE user_id = ? AND tab = ? AND project_id = ? ORDER BY id DESC LIMIT 200",
                (int(user["id"]), tab_norm, project_id),
            ).fetchall()
        elif tab_norm:
            rows = conn.execute(
                "SELECT * FROM canon_entries WHERE user_id = ? AND tab = ? ORDER BY id DESC LIMIT 200",
                (int(user["id"]), tab_norm),
            ).fetchall()
        elif project_id is not None:
            rows = conn.execute(
                "SELECT * FROM canon_entries WHERE user_id = ? AND project_id = ? ORDER BY id DESC LIMIT 200",
                (int(user["id"]), project_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM canon_entries WHERE user_id = ? ORDER BY id DESC LIMIT 200",
                (int(user["id"]),),
            ).fetchall()

        return {
            "items": [
                {
                    "id": r["id"],
                    "tab": r["tab"],
                    "title": r["title"],
                    "content": r["content"],
                    "project_id": r["project_id"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }
    finally:
        conn.close()


# =========================
# ROUTES: GENERATION
# =========================
@app.post("/generate")
def generate(req: GenerateRequest, request: Request, user: sqlite3.Row = Depends(get_current_user)):
    # Gate for paid access (trial/paid/owner)
    require_access(request, user)

    tab = normalize_tab(req.tab)
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    store_message(int(user["id"]), tab, "user", prompt)

    if not OPENAI_API_KEY or OpenAI is None:
        answer = (
            "⚠ OPENAI_API_KEY is not configured on this server.\n"
            "Set OPENAI_API_KEY in Render Environment Variables, then restart the service."
        )
        store_message(int(user["id"]), tab, "assistant", answer)
        return {"response": answer}

    client = OpenAI(api_key=OPENAI_API_KEY)
    history = load_recent_messages(int(user["id"]), tab, limit=18)
    system = system_prompt_for_tab(tab)

    # Build Responses API input safely
    input_msgs: List[Dict[str, Any]] = [{"role": "system", "content": system}]
    input_msgs.extend(history)

    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=input_msgs,
        )

        # Robust extraction
        text = ""
        if hasattr(resp, "output_text") and getattr(resp, "output_text"):
            text = getattr(resp, "output_text")  # type: ignore

        if not text:
            out = getattr(resp, "output", None)
            if out:
                for item in out:
                    for c in getattr(item, "content", []) or []:
                        if getattr(c, "type", "") in ("output_text", "text"):
                            text += getattr(c, "text", "") or ""

        if not text:
            text = "⚠ No text returned."

    except Exception as e:
        text = f"⚠ AI error: {str(e)}"

    store_message(int(user["id"]), tab, "assistant", text)

    # analytics
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


# IMPORTANT: alias routes so your UI can call either one without breaking
@app.post("/api/generate")
def api_generate(req: GenerateRequest, request: Request, user: sqlite3.Row = Depends(get_current_user)):
    return generate(req, request, user)


# =========================
# STRIPE: DEBUG
# =========================
@app.get("/debug/stripe")
def debug_stripe():
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

@app.get("/debug/owner")
def debug_owner():
    # safe debug: doesn't reveal the code, only whether it exists
    v = os.getenv("BOOKWORM_OWNER_CODE", "")
    return {
        "owner_env_present": bool(v),
        "owner_len": len(v) if v else 0,
    }

# =========================
# STRIPE: CHECKOUT + WEBHOOK
# =========================
class CheckoutRequest(BaseModel):
    plan: str  # basic/pro/patron

@app.post("/stripe/checkout")
def stripe_checkout(req: CheckoutRequest, request: Request, user: sqlite3.Row = Depends(get_current_user)):
    if not stripe_configured():
        raise HTTPException(status_code=500, detail="Stripe not configured on server.")
    price_id = price_id_for_plan(req.plan)
    if not price_id:
        raise HTTPException(status_code=400, detail="Invalid plan or missing price id.")

    base = FRONTEND_URL.strip() or str(request.base_url).rstrip("/")
    success_url = f"{base}/?checkout=success"
    cancel_url = f"{base}/?checkout=cancel"

    customer_email = user["email"]

    try:
        sess = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            allow_promotion_codes=True,
            customer_email=customer_email,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"user_id": str(user["id"]), "plan": req.plan},
        )
        return {"url": sess.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not stripe_configured() or not STRIPE_WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "detail": "Stripe not configured"}, status_code=500)

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(  # type: ignore
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception as e:
        return JSONResponse({"ok": False, "detail": f"Webhook error: {str(e)}"}, status_code=400)

    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    # We support: checkout.session.completed, customer.subscription.updated, customer.subscription.deleted
    conn = db_connect()
    try:
        if etype == "checkout.session.completed":
            customer_id = obj.get("customer", "") or ""
            subscription_id = obj.get("subscription", "") or ""
            email = obj.get("customer_details", {}).get("email", "") or obj.get("customer_email", "") or ""

            user_id = None
            # Prefer metadata user_id if present
            meta = obj.get("metadata", {}) or {}
            if meta.get("user_id"):
                try:
                    user_id = int(meta["user_id"])
                except Exception:
                    user_id = None

            if user_id is None and email:
                u = conn.execute("SELECT id FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
                if u:
                    user_id = int(u["id"])

            plan = (meta.get("plan") or "basic").strip().lower()
            status = "trialing"

            if user_id is not None:
                # store customer id on user
                conn.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?", (customer_id, user_id))
                conn.execute(
                    "INSERT INTO subscriptions (user_id, stripe_customer_id, stripe_subscription_id, status, plan, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, customer_id, subscription_id, status, plan, now_utc_iso()),
                )
                conn.commit()

        elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
            customer_id = obj.get("customer", "") or ""
            subscription_id = obj.get("id", "") or ""
            status = (obj.get("status", "") or "none").lower()
            plan = "basic"
            try:
                items = obj.get("items", {}).get("data", []) or []
                if items:
                    price = items[0].get("price", {})
                    # If you want: map price.id -> plan
                    pid = (price.get("id") or "").strip()
                    if pid == STRIPE_PRO_PRICE_ID:
                        plan = "pro"
                    elif pid == STRIPE_PATRON_PRICE_ID:
                        plan = "patron"
                    else:
                        plan = "basic"
            except Exception:
                pass

            # Find user by customer id
            u = conn.execute("SELECT id FROM users WHERE stripe_customer_id = ?", (customer_id,)).fetchone()
            if u:
                user_id = int(u["id"])
                # Upsert subscription row
                existing = conn.execute(
                    "SELECT id FROM subscriptions WHERE user_id = ? AND stripe_subscription_id = ?",
                    (user_id, subscription_id),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE subscriptions SET status = ?, plan = ? WHERE id = ?",
                        (status, plan, int(existing["id"])),
                    )
                else:
                    conn.execute(
                        "INSERT INTO subscriptions (user_id, stripe_customer_id, stripe_subscription_id, status, plan, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (user_id, customer_id, subscription_id, status, plan, now_utc_iso()),
                    )
                conn.commit()

        return {"ok": True}

    finally:
        conn.close()

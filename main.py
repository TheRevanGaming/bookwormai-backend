import os
import json
import sqlite3
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Any, Dict, List

from fastapi import FastAPI, HTTPException, Depends, Response, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext

# Optional OpenAI
try:
    from openai import OpenAI  # type: ignore
except Exception:
    OpenAI = None  # type: ignore

# Optional Stripe
try:
    import stripe  # type: ignore
except Exception:
    stripe = None  # type: ignore


APP_NAME = "Book Worm AI"

# ========= ENV =========
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "bookworm.db"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

BOOKWORM_OWNER_CODE = os.getenv("BOOKWORM_OWNER_CODE", "")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_BASIC_PRICE_ID = os.getenv("STRIPE_BASIC_PRICE_ID", "")
STRIPE_PRO_PRICE_ID = os.getenv("STRIPE_PRO_PRICE_ID", "")
STRIPE_PATRON_PRICE_ID = os.getenv("STRIPE_PATRON_PRICE_ID", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")  # e.g. https://bookwormai-backend-t8uv.onrender.com

COOKIE_NAME = "bw_session"
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))

ALLOWED_ORIGINS = [
    "https://therevangaming.github.io",
    "http://127.0.0.1:5050",
    "http://localhost:5050",
]

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ========= APP =========
app = FastAPI(title=APP_NAME)

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ========= DB =========
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def iso_in_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    schema_path = os.path.join(os.path.dirname(__file__), "db", "schema.sql")
    if not os.path.exists(schema_path):
        return
    conn = db_connect()
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
        print("[init_db] Applied schema.sql successfully.")
    except Exception as e:
        print("[init_db] Error applying schema:", e)
    finally:
        conn.close()


# ========= MODELS =========
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class GenerateRequest(BaseModel):
    tab: str
    prompt: str
    project: Optional[str] = None

class SaveCanonRequest(BaseModel):
    tab: str
    title: str
    content: str
    project: Optional[str] = None

class OwnerUnlockRequest(BaseModel):
    code: str

class CheckoutRequest(BaseModel):
    plan: str  # basic | pro | patron


# ========= HELPERS =========
def hash_password(pw: str) -> str:
    return pwd_context.hash(pw)

def verify_password(pw: str, pw_hash: str) -> bool:
    try:
        return pwd_context.verify(pw, pw_hash)
    except Exception:
        return False

def normalize_tab(tab: str) -> str:
    t = (tab or "").strip().lower()
    aliases = {
        "music": "musicdev",
        "game": "gamedev",
        "image": "imagelab",
        "voice": "voicelab",
        "designer": "gamedesigner",
        "writing": "writing",
        "book": "writing",
        "story": "writing",
        "chat": "chat",
    }
    return aliases.get(t, t or "chat")

def system_prompt_for_tab(tab: str) -> str:
    base = (
        "You are Book Worm AI Studio. Be direct, helpful, and consistent.\n"
        "Always continue from the user's last message without re-asking what they already said.\n"
        "If you offer options and the user picks one, proceed immediately.\n"
        "Be practical and implementation-oriented.\n"
    )
    tab = normalize_tab(tab)
    if tab == "musicdev":
        return base + "Focus on music production, songwriting, mixing, sound design, and creative direction."
    if tab == "gamedev":
        return base + "Focus on game design, systems, mechanics, UE/UEFN workflows, quests, balancing, and implementation steps."
    if tab == "imagelab":
        return base + "Focus on image prompts, art direction, composition, style consistency, and visual pipelines."
    if tab == "voicelab":
        return base + "Focus on voice acting workflows, dialogue writing, narration, casting/AI voice pipelines."
    if tab == "gamedesigner":
        return base + "Focus on tabletop/board/card game design, rule systems, balancing, and exportable content."
    if tab == "writing":
        return base + "Focus on storytelling, outlines, prose, continuity, and canon-friendly expansions."
    return base + "General chat mode."

def ensure_project(conn: sqlite3.Connection, user_id: int, project_name: Optional[str]) -> Optional[int]:
    if not project_name:
        return None
    name = project_name.strip()
    if not name:
        return None
    row = conn.execute(
        "SELECT id FROM projects WHERE user_id=? AND name=?",
        (user_id, name),
    ).fetchone()
    if row:
        return int(row["id"])
    conn.execute(
        "INSERT INTO projects (user_id, name, created_at) VALUES (?, ?, ?)",
        (user_id, name, now_utc_iso()),
    )
    conn.commit()
    row2 = conn.execute(
        "SELECT id FROM projects WHERE user_id=? AND name=?",
        (user_id, name),
    ).fetchone()
    return int(row2["id"]) if row2 else None

def set_session_cookie(resp: Response, token: str) -> None:
    # SameSite=Lax works well for normal usage
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=bool(PUBLIC_BASE_URL.startswith("https://")),
        samesite="lax",
        max_age=SESSION_DAYS * 24 * 60 * 60,
        path="/",
    )

def clear_session_cookie(resp: Response) -> None:
    resp.delete_cookie(COOKIE_NAME, path="/")


# ========= AUTH DEPENDENCIES =========
def get_current_session(request: Request) -> Optional[sqlite3.Row]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT token, user_id, is_owner, expires_at FROM sessions WHERE token=?",
            (token,),
        ).fetchone()
        if not row:
            return None
        # expiry check
        try:
            exp = datetime.fromisoformat(row["expires_at"])
            if exp < datetime.now(timezone.utc):
                conn.execute("DELETE FROM sessions WHERE token=?", (token,))
                conn.commit()
                return None
        except Exception:
            return None
        return row
    finally:
        conn.close()

def get_current_user(request: Request) -> sqlite3.Row:
    sess = get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail="Not logged in")
    conn = db_connect()
    try:
        u = conn.execute("SELECT * FROM users WHERE id=?", (int(sess["user_id"]),)).fetchone()
        if not u:
            raise HTTPException(status_code=401, detail="Not logged in")
        return u
    finally:
        conn.close()

def require_owner(request: Request) -> sqlite3.Row:
    sess = get_current_session(request)
    if not sess or int(sess["is_owner"]) != 1:
        raise HTTPException(status_code=403, detail="Owner/Admin not unlocked")
    conn = db_connect()
    try:
        u = conn.execute("SELECT * FROM users WHERE id=?", (int(sess["user_id"]),)).fetchone()
        if not u:
            raise HTTPException(status_code=401, detail="Not logged in")
        return u
    finally:
        conn.close()


# ========= STARTUP =========
@app.on_event("startup")
def on_startup():
    init_db()
    if stripe is not None and STRIPE_SECRET_KEY:
        stripe.api_key = STRIPE_SECRET_KEY


# ========= STATIC HOME =========
@app.get("/", response_class=HTMLResponse)
def home():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Book Worm AI</h1><p>Missing static/index.html</p>")

@app.get("/health")
def health():
    return {"ok": True, "app": APP_NAME, "time": now_utc_iso()}


# ========= SETTINGS =========
@app.get("/api/settings")
def api_settings(request: Request):
    sess = get_current_session(request)
    me = {"logged_in": False}
    if sess:
        conn = db_connect()
        try:
            u = conn.execute("SELECT email FROM users WHERE id=?", (int(sess["user_id"]),)).fetchone()
            if u:
                me = {
                    "logged_in": True,
                    "email": u["email"],
                    "is_owner": bool(int(sess["is_owner"])),
                }
        finally:
            conn.close()

    tabs = ["chat", "writing", "gamedev", "musicdev", "imagelab", "voicelab", "gamedesigner"]

    return {
        "app": APP_NAME,
        "studio_url": PUBLIC_BASE_URL or "http://127.0.0.1:5050",
        "tabs": tabs,
        "stripe_ready": bool(STRIPE_SECRET_KEY and stripe is not None and STRIPE_BASIC_PRICE_ID and STRIPE_PRO_PRICE_ID and STRIPE_PATRON_PRICE_ID),
        "me": me,
    }


# ========= AUTH ROUTES =========
@app.post("/auth/register")
def auth_register(req: RegisterRequest):
    conn = db_connect()
    try:
        existing = conn.execute("SELECT id FROM users WHERE email=?", (req.email.lower(),)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered")

        pw_hash = hash_password(req.password)
        conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (req.email.lower(), pw_hash, now_utc_iso()),
        )
        conn.commit()

        # create starter subscription record
        user = conn.execute("SELECT id FROM users WHERE email=?", (req.email.lower(),)).fetchone()
        if user:
            conn.execute(
                "INSERT OR REPLACE INTO subscriptions (user_id, plan, status, updated_at) VALUES (?, 'free', 'active', ?)",
                (int(user["id"]), now_utc_iso()),
            )
            conn.commit()

        return {"ok": True}
    finally:
        conn.close()

@app.post("/auth/login")
def auth_login(req: LoginRequest, response: Response):
    conn = db_connect()
    try:
        u = conn.execute("SELECT * FROM users WHERE email=?", (req.email.lower(),)).fetchone()
        if not u or not verify_password(req.password, u["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO sessions (token, user_id, is_owner, created_at, expires_at) VALUES (?, ?, 0, ?, ?)",
            (token, int(u["id"]), now_utc_iso(), iso_in_days(SESSION_DAYS)),
        )
        conn.commit()

        set_session_cookie(response, token)
        return {"ok": True, "email": u["email"]}
    finally:
        conn.close()

@app.post("/auth/logout")
def auth_logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        conn = db_connect()
        try:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            conn.commit()
        finally:
            conn.close()
    clear_session_cookie(response)
    return {"ok": True}

@app.get("/auth/me")
def auth_me(request: Request):
    sess = get_current_session(request)
    if not sess:
        return {"logged_in": False}
    conn = db_connect()
    try:
        u = conn.execute("SELECT email FROM users WHERE id=?", (int(sess["user_id"]),)).fetchone()
        if not u:
            return {"logged_in": False}
        return {"logged_in": True, "email": u["email"], "is_owner": bool(int(sess["is_owner"]))}
    finally:
        conn.close()


# ========= OWNER / ADMIN =========
@app.post("/owner/unlock")
def owner_unlock(req: OwnerUnlockRequest, request: Request, response: Response):
    # must be logged in first
    sess = get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail="Not logged in")

    if not BOOKWORM_OWNER_CODE:
        raise HTTPException(status_code=500, detail="BOOKWORM_OWNER_CODE not configured on server")

    if (req.code or "").strip() != BOOKWORM_OWNER_CODE.strip():
        raise HTTPException(status_code=403, detail="Invalid owner code")

    conn = db_connect()
    try:
        conn.execute("UPDATE sessions SET is_owner=1 WHERE token=?", (sess["token"],))
        conn.commit()
    finally:
        conn.close()

    return {"ok": True}

@app.post("/owner/lock")
def owner_lock(request: Request):
    sess = get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail="Not logged in")
    conn = db_connect()
    try:
        conn.execute("UPDATE sessions SET is_owner=0 WHERE token=?", (sess["token"],))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}

@app.get("/debug/owner")
def debug_owner():
    # Safe debug: no secret revealed
    v = os.getenv("BOOKWORM_OWNER_CODE", "")
    return {"owner_env_present": bool(v), "owner_len": len(v) if v else 0}


# ========= MESSAGES / HISTORY =========
def store_message(user_id: int, tab: str, role: str, content: str, project_id: Optional[int]) -> None:
    conn = db_connect()
    try:
        conn.execute(
            "INSERT INTO messages (user_id, project_id, tab, role, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, project_id, tab, role, content, now_utc_iso()),
        )
        conn.commit()
    finally:
        conn.close()

def load_recent_messages(user_id: int, tab: str, project_id: Optional[int], limit: int = 18) -> List[Dict[str, Any]]:
    conn = db_connect()
    try:
        if project_id is None:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE user_id=? AND tab=? AND project_id IS NULL ORDER BY id DESC LIMIT ?",
                (user_id, tab, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE user_id=? AND tab=? AND project_id=? ORDER BY id DESC LIMIT ?",
                (user_id, tab, project_id, limit),
            ).fetchall()
        items = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        return items
    finally:
        conn.close()


# ========= CANON =========
@app.post("/canon/save")
def canon_save(req: SaveCanonRequest, user: sqlite3.Row = Depends(get_current_user)):
    tab = normalize_tab(req.tab)
    if not req.title.strip() or not req.content.strip():
        raise HTTPException(status_code=400, detail="title and content required")

    conn = db_connect()
    try:
        project_id = ensure_project(conn, int(user["id"]), req.project)
        conn.execute(
            "INSERT INTO canon_items (user_id, project_id, tab, title, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (int(user["id"]), project_id, tab, req.title.strip(), req.content.strip(), now_utc_iso()),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True}

@app.get("/canon/list")
def canon_list(tab: str = "chat", project: Optional[str] = None, user: sqlite3.Row = Depends(get_current_user)):
    tab = normalize_tab(tab)
    conn = db_connect()
    try:
        pid = ensure_project(conn, int(user["id"]), project) if project else None
        if pid is None:
            rows = conn.execute(
                "SELECT id, tab, title, content, created_at FROM canon_items WHERE user_id=? AND tab=? AND project_id IS NULL ORDER BY id DESC LIMIT 200",
                (int(user["id"]), tab),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, tab, title, content, created_at FROM canon_items WHERE user_id=? AND tab=? AND project_id=? ORDER BY id DESC LIMIT 200",
                (int(user["id"]), tab, pid),
            ).fetchall()
        return {"items": [dict(r) for r in rows]}
    finally:
        conn.close()


# ========= GENERATE =========
@app.post("/generate")
def generate(req: GenerateRequest, user: sqlite3.Row = Depends(get_current_user)):
    tab = normalize_tab(req.tab)
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    conn = db_connect()
    try:
        project_id = ensure_project(conn, int(user["id"]), req.project)
    finally:
        conn.close()

    store_message(int(user["id"]), tab, "user", prompt, project_id)

    if not OPENAI_API_KEY or OpenAI is None:
        text = (
            "⚠ OPENAI_API_KEY is not configured on this server.\n"
            "Set OPENAI_API_KEY in Render Environment Variables, then redeploy."
        )
        store_message(int(user["id"]), tab, "assistant", text, project_id)
        return {"response": text}

    client = OpenAI(api_key=OPENAI_API_KEY)
    history = load_recent_messages(int(user["id"]), tab, project_id, limit=18)
    system = system_prompt_for_tab(tab)

    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": system},
                *history
            ],
        )
        try:
            text = resp.output_text  # type: ignore
        except Exception:
            text = ""
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

    store_message(int(user["id"]), tab, "assistant", text, project_id)

    conn2 = db_connect()
    try:
        conn2.execute(
            "INSERT INTO analytics_events (user_id, event, meta_json, created_at) VALUES (?, 'generate', ?, ?)",
            (int(user["id"]), json.dumps({"tab": tab}), now_utc_iso()),
        )
        conn2.commit()
    finally:
        conn2.close()

    return {"response": text}


# ========= STRIPE =========
def stripe_configured() -> bool:
    return bool(STRIPE_SECRET_KEY) and (stripe is not None)

def price_id_for_plan(plan: str) -> str:
    p = (plan or "").strip().lower()
    if p == "basic":
        return STRIPE_BASIC_PRICE_ID
    if p == "pro":
        return STRIPE_PRO_PRICE_ID
    if p == "patron":
        return STRIPE_PATRON_PRICE_ID
    return ""

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

@app.post("/stripe/create-checkout-session")
def stripe_create_checkout(req: CheckoutRequest, request: Request, user: sqlite3.Row = Depends(get_current_user)):
    if not stripe_configured():
        raise HTTPException(status_code=500, detail="Stripe not configured on server")

    price_id = price_id_for_plan(req.plan)
    if not price_id:
        raise HTTPException(status_code=400, detail="Missing price id for plan")

    # Where to return after checkout
    base = PUBLIC_BASE_URL or "http://127.0.0.1:5050"
    success_url = f"{base}/?stripe=success"
    cancel_url = f"{base}/?stripe=cancel"

    # get or create customer id
    conn = db_connect()
    try:
        sub = conn.execute("SELECT stripe_customer_id FROM subscriptions WHERE user_id=?", (int(user["id"]),)).fetchone()
        customer_id = sub["stripe_customer_id"] if sub else None
        if not customer_id:
            cust = stripe.Customer.create(email=user["email"])
            customer_id = cust["id"]
            conn.execute(
                "INSERT OR REPLACE INTO subscriptions (user_id, plan, stripe_customer_id, status, updated_at) VALUES (?, 'free', ?, 'active', ?)",
                (int(user["id"]), customer_id, now_utc_iso()),
            )
            conn.commit()
    finally:
        conn.close()

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        allow_promotion_codes=True,
    )
    return {"url": session["url"]}

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not stripe_configured() or not STRIPE_WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "detail": "Stripe webhook not configured"}, status_code=500)

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig, secret=STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return JSONResponse({"ok": False, "detail": f"Webhook error: {str(e)}"}, status_code=400)

    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    # Subscription updates
    if etype in ("checkout.session.completed", "customer.subscription.created", "customer.subscription.updated"):
        customer_id = obj.get("customer") or obj.get("customer_id")
        subscription_id = obj.get("subscription") or obj.get("id")
        status = obj.get("status", "active")

        conn = db_connect()
        try:
            row = conn.execute("SELECT user_id FROM subscriptions WHERE stripe_customer_id=?", (customer_id,)).fetchone()
            if row:
                plan = "paid"
                conn.execute(
                    "UPDATE subscriptions SET plan=?, stripe_subscription_id=?, status=?, updated_at=? WHERE user_id=?",
                    (plan, subscription_id, status, now_utc_iso(), int(row["user_id"])),
                )
                conn.commit()
        finally:
            conn.close()

    return {"ok": True}


# ========= ADMIN =========
@app.get("/admin/analytics")
def admin_analytics(owner: sqlite3.Row = Depends(require_owner)):
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT event, COUNT(*) as c FROM analytics_events GROUP BY event ORDER BY c DESC"
        ).fetchall()
        return {"events": [dict(r) for r in rows]}
    finally:
        conn.close()

@app.get("/admin/users")
def admin_users(owner: sqlite3.Row = Depends(require_owner)):
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT id, email, created_at FROM users ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"users": [dict(r) for r in rows]}
    finally:
        conn.close()

@app.get("/admin/subscriptions")
def admin_subscriptions(owner: sqlite3.Row = Depends(require_owner)):
    conn = db_connect()
    try:
        rows = conn.execute(
            """
            SELECT u.email, s.plan, s.status, s.updated_at
            FROM subscriptions s
            JOIN users u ON u.id = s.user_id
            ORDER BY s.updated_at DESC
            LIMIT 200
            """
        ).fetchall()
        return {"subs": [dict(r) for r in rows]}
    finally:
        conn.close()

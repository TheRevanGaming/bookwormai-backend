import os
import sqlite3
import secrets
import hashlib
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List

import stripe
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

from openai import OpenAI

# -----------------------------------------------------------------------------
# CONFIG / ENV
# -----------------------------------------------------------------------------

APP_ROOT = Path(__file__).parent
DB_PATH = APP_ROOT / "bookworm.db"
SCHEMA_PATH = APP_ROOT / "db" / "schema.sql"
SYSTEM_PROMPT_PATH = APP_ROOT / "prompts" / "system_prompt.txt"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set in environment.")

BOOKWORM_OWNER_CODE = os.getenv("BOOKWORM_OWNER_CODE", "")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:5050")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

client = OpenAI(api_key=OPENAI_API_KEY)

# Simple global salt for password hashing
PASSWORD_SALT = os.getenv("BOOKWORM_PW_SALT", "bookworm-default-salt")

# Plans we recognize (match your Stripe setup)
VALID_PLANS = ["free", "basic", "pro", "patron"]


# -----------------------------------------------------------------------------
# DB HELPERS
# -----------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not SCHEMA_PATH.exists():
        print("[init_db] schema.sql not found, skipping.")
        return
    conn = get_db()
    try:
        with SCHEMA_PATH.open("r", encoding="utf-8") as f:
            sql = f.read()
        conn.executescript(sql)
        conn.commit()
        print("[init_db] Applied schema.sql successfully.")
    except Exception as e:
        print(f"[init_db] Error applying schema: {e}")
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# PASSWORD / AUTH HELPERS
# -----------------------------------------------------------------------------

def hash_password(email: str, password: str) -> str:
    data = (email.strip().lower() + "::" + password + "::" + PASSWORD_SALT).encode(
        "utf-8"
    )
    return hashlib.sha256(data).hexdigest()


def verify_password(email: str, password: str, stored_hash: str) -> bool:
    return hash_password(email, password) == stored_hash


def create_session(user_id: int, days_valid: int = 30) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    expires = now + timedelta(days=days_valid)

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sessions (user_id, token, created_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            token,
            now.isoformat(),
            expires.isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    return token


def get_user_from_token(token: str) -> Optional[dict]:
    if not token:
        return None

    now = datetime.utcnow().isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            u.id as user_id,
            u.email,
            u.is_owner,
            s.token,
            s.expires_at
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token = ?
        """,
        (token,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None

    if row["expires_at"] < now:
        # Session expired
        return None

    return {
        "id": row["user_id"],
        "email": row["email"],
        "is_owner": bool(row["is_owner"]),
    }


async def auth_dependency(request: Request) -> Optional[dict]:
    """
    Returns dict with user info if logged in, else None.
    We do NOT hard-block generation here to keep beta friendly.
    """
    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if not token:
        # Also check cookie, if any
        token = request.cookies.get("session_token", "")

    if not token:
        return None

    user = get_user_from_token(token)
    return user


async def owner_dependency(user: dict = Depends(auth_dependency)) -> dict:
    if user is None or not user.get("is_owner"):
        raise HTTPException(status_code=403, detail="Owner access required.")
    return user


# -----------------------------------------------------------------------------
# STRIPE HELPERS
# -----------------------------------------------------------------------------

def upsert_subscription_from_stripe(
    user_id: int,
    plan: str,
    status: str,
    stripe_customer_id: Optional[str],
    stripe_subscription_id: Optional[str],
    current_period_end: Optional[int],
) -> None:
    now = datetime.utcnow().isoformat()
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT id FROM subscriptions WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    if row:
        cur.execute(
            """
            UPDATE subscriptions
            SET
              plan = ?,
              status = ?,
              stripe_customer_id = ?,
              stripe_subscription_id = ?,
              current_period_end = ?,
              updated_at = ?
            WHERE user_id = ?
            """,
            (
                plan,
                status,
                stripe_customer_id,
                stripe_subscription_id,
                current_period_end,
                now,
                user_id,
            ),
        )
    else:
        cur.execute(
            """
            INSERT INTO subscriptions
              (user_id, plan, status, stripe_customer_id, stripe_subscription_id,
               current_period_end, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                plan,
                status,
                stripe_customer_id,
                stripe_subscription_id,
                current_period_end,
                now,
                now,
            ),
        )
    conn.commit()
    conn.close()


def log_stripe_event(event_id: str, event_type: str, payload: dict) -> None:
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT OR IGNORE INTO stripe_events (event_id, type, payload, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                event_id,
                event_type,
                json.dumps(payload),
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Pydantic MODELS
# -----------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    owner_code: Optional[str] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    token: str
    email: EmailStr
    is_owner: bool
    plan: str = "free"


class MeResponse(BaseModel):
    email: EmailStr
    is_owner: bool
    plan: str
    created_at: Optional[str] = None


class GenerateRequest(BaseModel):
    prompt: str
    mode: str = "auto"  # 'auto', 'lore', 'world', 'character', etc.
    depth: str = "deep"  # 'deep', 'fast'
    project_id: Optional[int] = None


class GenerateResponse(BaseModel):
    response: str


class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None


class Project(BaseModel):
    id: int
    name: str
    description: Optional[str] = None


class DocCreate(BaseModel):
    project_id: int
    title: str
    body: str
    tags: Optional[List[str]] = None
    canon_state: str = "LOCKED_CANON"
    source: str = "manual-import"


class Doc(BaseModel):
    id: int
    project_id: int
    title: str
    body: str
    tags: Optional[List[str]] = None
    canon_state: str
    source: Optional[str] = None


class StripeCheckoutRequest(BaseModel):
    plan: str  # 'basic', 'pro', 'patron'
    success_url: str
    cancel_url: str


class StripeCheckoutResponse(BaseModel):
    checkout_url: str


class AdminSubscribersResponse(BaseModel):
    data: List[dict]


class AdminUsageResponse(BaseModel):
    total_generations: int
    events: List[dict]


# -----------------------------------------------------------------------------
# MODE / PROMPT HELPERS
# -----------------------------------------------------------------------------

def infer_mode_from_prompt(prompt: str) -> str:
    text = prompt.lower()
    # Music / lyrics
    if any(k in text for k in ["lyrics", "chorus", "verse", "hook"]):
        return "lyrics"
    if any(k in text for k in ["beat", "808", "instrumental", "ost"]):
        return "instrumental_concept"
    if any(k in text for k in ["sound effect", "sfx", "ambience", "audio design"]):
        return "audio_concept"

    # Characters
    if "character sheet" in text or "character profile" in text:
        return "character"
    if "backstory" in text or "origin story" in text:
        return "character"
    if "build me a character" in text or "design a character" in text:
        return "character"

    # Worlds / realms / biomes / flora-fauna
    if any(k in text for k in ["realm", "continent", "planet", "world map"]):
        return "world"
    if any(k in text for k in ["biome", "region", "climate"]):
        return "world"
    if any(k in text for k in ["flora", "fauna", "creature", "monster", "species"]):
        return "world"
    if any(k in text for k in ["culture", "tribe", "kingdom", "empire"]):
        return "world"
    if any(k in text for k in ["magic system", "power system", "tech system"]):
        return "world"

    # Lore
    if any(k in text for k in ["lore", "canon", "timeline"]):
        return "lore"
    if "summarize my world" in text or "explain my world" in text:
        return "lore"

    # Scenes / blocks
    if "rewrite this" in text or "edit this scene" in text:
        return "block"
    if "write a scene" in text or "write a chapter" in text:
        return "block"

    return "free"


def load_system_prompt() -> str:
    if not SYSTEM_PROMPT_PATH.exists():
        return "You are The Book Worm AI."
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


SYSTEM_PROMPT = load_system_prompt()


def record_usage_event(user_id: Optional[int], event_type: str) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO usage_events (user_id, event_type, created_at)
        VALUES (?, ?, ?)
        """,
        (
            user_id,
            event_type,
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def get_user_plan(user: Optional[dict]) -> str:
    if user is None:
        return "free"
    if user.get("is_owner"):
        return "owner"
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT plan, status FROM subscriptions WHERE user_id = ?",
        (user["id"],),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return "free"
    if row["status"] != "active":
        return "free"
    plan = row["plan"] or "free"
    if plan not in VALID_PLANS and plan != "owner":
        return "free"
    return plan


# -----------------------------------------------------------------------------
# FASTAPI APP
# -----------------------------------------------------------------------------

app = FastAPI(title="Book Worm AI", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_ORIGIN,
        "http://localhost:5050",
        "http://127.0.0.1:5050",
        "https://bookwormai-backend-t8uv.onrender.com",
        "https://therevangaming.github.io",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Serve /static
static_dir = APP_ROOT / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    print("[startup] System prompt loaded.")


# -----------------------------------------------------------------------------
# ROOT: serve index.html
# -----------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    index_path = static_dir / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>Book Worm AI Backend</h1>", status_code=200)
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


# -----------------------------------------------------------------------------
# AUTH ENDPOINTS
# -----------------------------------------------------------------------------

@app.post("/register", response_model=AuthResponse)
async def register(payload: RegisterRequest) -> AuthResponse:
    email = payload.email.strip().lower()
    pw_hash = hash_password(email, payload.password)

    is_owner = 0
    if BOOKWORM_OWNER_CODE and payload.owner_code == BOOKWORM_OWNER_CODE:
        is_owner = 1

    conn = get_db()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    try:
        cur.execute(
            """
            INSERT INTO users (email, password_hash, is_owner, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (email, pw_hash, is_owner, now),
        )
        user_id = cur.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Email already registered.")
    conn.close()

    token = create_session(user_id)
    plan = "owner" if is_owner else "free"
    return AuthResponse(token=token, email=email, is_owner=bool(is_owner), plan=plan)


@app.post("/login", response_model=AuthResponse)
async def login(payload: LoginRequest) -> AuthResponse:
    email = payload.email.strip().lower()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, email, password_hash, is_owner, created_at
        FROM users
        WHERE email = ?
        """,
        (email,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    if not verify_password(email, payload.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    token = create_session(row["id"])
    temp_user = {"id": row["id"], "email": row["email"], "is_owner": bool(row["is_owner"])}
    plan = get_user_plan(temp_user)

    return AuthResponse(
        token=token,
        email=row["email"],
        is_owner=bool(row["is_owner"]),
        plan=plan,
    )


@app.get("/me", response_model=MeResponse)
async def me(user: Optional[dict] = Depends(auth_dependency)) -> MeResponse:
    if user is None:
        return MeResponse(email="anonymous@example.com", is_owner=False, plan="free")

    plan = get_user_plan(user)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT created_at FROM users WHERE id = ?",
        (user["id"],),
    )
    row = cur.fetchone()
    conn.close()
    created_at = row["created_at"] if row else None

    return MeResponse(
        email=user["email"],
        is_owner=bool(user["is_owner"]),
        plan=plan,
        created_at=created_at,
    )


# -----------------------------------------------------------------------------
# PROJECTS & DOCS (CANON)
# -----------------------------------------------------------------------------

@app.post("/projects", response_model=Project)
async def create_project(payload: ProjectCreate, user: Optional[dict] = Depends(auth_dependency)) -> Project:
    conn = get_db()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    user_id = user["id"] if user else None
    cur.execute(
        """
        INSERT INTO projects (user_id, name, description, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, payload.name, payload.description, now),
    )
    project_id = cur.lastrowid
    conn.commit()
    conn.close()
    return Project(id=project_id, name=payload.name, description=payload.description)


@app.get("/projects", response_model=List[Project])
async def list_projects(user: Optional[dict] = Depends(auth_dependency)) -> List[Project]:
    conn = get_db()
    cur = conn.cursor()
    # Everyone sees their own and global projects (user_id null)
    uid = user["id"] if user else None
    if uid is not None:
        cur.execute(
            """
            SELECT id, name, description
            FROM projects
            WHERE user_id = ? OR user_id IS NULL
            ORDER BY created_at DESC
            """,
            (uid,),
        )
    else:
        cur.execute(
            """
            SELECT id, name, description
            FROM projects
            ORDER BY created_at DESC
            """
        )
    rows = cur.fetchall()
    conn.close()
    return [
        Project(id=row["id"], name=row["name"], description=row["description"])
        for row in rows
    ]


@app.post("/docs", response_model=Doc)
async def create_doc(payload: DocCreate, user: Optional[dict] = Depends(auth_dependency)) -> Doc:
    tags_str = ",".join(payload.tags) if payload.tags else None
    now = datetime.utcnow().isoformat()

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO docs (project_id, title, body, tags, canon_state, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.project_id,
            payload.title,
            payload.body,
            tags_str,
            payload.canon_state,
            payload.source,
            now,
        ),
    )
    doc_id = cur.lastrowid
    conn.commit()
    conn.close()

    return Doc(
        id=doc_id,
        project_id=payload.project_id,
        title=payload.title,
        body=payload.body,
        tags=payload.tags,
        canon_state=payload.canon_state,
        source=payload.source,
    )


@app.get("/docs", response_model=List[Doc])
async def list_docs(project_id: Optional[int] = None, user: Optional[dict] = Depends(auth_dependency)) -> List[Doc]:
    conn = get_db()
    cur = conn.cursor()
    if project_id is not None:
        cur.execute(
            """
            SELECT id, project_id, title, body, tags, canon_state, source
            FROM docs
            WHERE project_id = ?
            ORDER BY created_at DESC
            """,
            (project_id,),
        )
    else:
        cur.execute(
            """
            SELECT id, project_id, title, body, tags, canon_state, source
            FROM docs
            ORDER BY created_at DESC
            """
        )
    rows = cur.fetchall()
    conn.close()

    docs: List[Doc] = []
    for row in rows:
        tags = row["tags"].split(",") if row["tags"] else None
        docs.append(
            Doc(
                id=row["id"],
                project_id=row["project_id"],
                title=row["title"],
                body=row["body"],
                tags=tags,
                canon_state=row["canon_state"],
                source=row["source"],
            )
        )
    return docs


# -----------------------------------------------------------------------------
# GENERATE (OpenAI chat)
# -----------------------------------------------------------------------------

@app.post("/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest, user: Optional[dict] = Depends(auth_dependency)) -> GenerateResponse:
    """
    Core generation endpoint.
    Uses:
      - SYSTEM_PROMPT
      - optional canon from docs (if project_id provided)
      - optional user auth to track usage
    """
    record_usage_event(user["id"] if user else None, "generate")

    # Resolve mode
    resolved_mode = req.mode
    if req.mode == "auto":
        resolved_mode = infer_mode_from_prompt(req.prompt)

    # Pull canon if project specified
    canon_text = ""
    if req.project_id is not None:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT title, body
            FROM docs
            WHERE project_id = ?
            ORDER BY created_at DESC
            LIMIT 8
            """,
            (req.project_id,),
        )
        rows = cur.fetchall()
        conn.close()
        parts = []
        for row in rows:
            parts.append(f"# {row['title']}\n{row['body']}")
        canon_text = "\n\n".join(parts)
        if len(canon_text) > 6000:
            canon_text = canon_text[:6000] + "\n\n...[canon truncated]..."

    mode_line = f"Mode: {resolved_mode}"
    depth_line = f"Depth: {req.depth}"

    user_content = f"{mode_line}\n{depth_line}\n\nUser prompt:\n{req.prompt}"

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if canon_text:
        messages.append(
            {
                "role": "system",
                "content": (
                    "The following is LOCKED CANON for this project. "
                    "You MUST obey it and avoid contradictions:\n\n"
                    f"{canon_text}"
                ),
            }
        )

    messages.append({"role": "user", "content": user_content})

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.8,
        )
        answer = completion.choices[0].message.content or ""
        return GenerateResponse(response=answer)
    except Exception as e:
        return GenerateResponse(
            response=(
                "⚠ Book Worm hit an error talking to OpenAI.\n\n"
                f"Error type: {e.__class__.__name__}\n"
                f"Details: {e}"
            )
        )


# -----------------------------------------------------------------------------
# STRIPE CHECKOUT + WEBHOOK
# -----------------------------------------------------------------------------

@app.post("/stripe/create-checkout-session", response_model=StripeCheckoutResponse)
async def stripe_create_checkout_session(
    payload: StripeCheckoutRequest,
    user: dict = Depends(auth_dependency),
):
    if user is None:
        raise HTTPException(status_code=401, detail="Login required.")

    if payload.plan not in ["basic", "pro", "patron"]:
        raise HTTPException(status_code=400, detail="Invalid plan.")

    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe not configured on server.")

    # You MUST set these env vars in Render / locally
    price_env_name = {
        "basic": "STRIPE_BASIC_PRICE_ID",
        "pro": "STRIPE_PRO_PRICE_ID",
        "patron": "STRIPE_PATRON_PRICE_ID",
    }[payload.plan]
    price_id = os.getenv(price_env_name)
    if not price_id:
        raise HTTPException(
            status_code=500,
            detail=f"{price_env_name} is not set on server.",
        )

    try:
        session = stripe.checkout.Session.create(
            success_url=payload.success_url,
            cancel_url=payload.cancel_url,
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            metadata={"user_id": str(user["id"]), "plan": payload.plan},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {e}")

    return StripeCheckoutResponse(checkout_url=session.url)


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Stripe webhook secret not set.")

    signature = request.headers.get("stripe-signature", "")
    body = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload=body,
            sig_header=signature,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid webhook: {e}")

    event_id = event.get("id")
    event_type = event.get("type")
    data = event.get("data", {})
    obj = data.get("object", {})

    log_stripe_event(event_id, event_type, event)

    # Handle relevant events
    if event_type == "checkout.session.completed":
        mode = obj.get("mode")
        if mode == "subscription":
            metadata = obj.get("metadata") or {}
            user_id_str = metadata.get("user_id")
            plan = metadata.get("plan", "basic")
            subscription_id = obj.get("subscription")
            customer_id = obj.get("customer")

            if user_id_str and user_id_str.isdigit():
                user_id = int(user_id_str)
                upsert_subscription_from_stripe(
                    user_id=user_id,
                    plan=plan,
                    status="active",
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=subscription_id,
                    current_period_end=None,
                )

    elif event_type == "customer.subscription.deleted":
        sub = obj
        customer_id = sub.get("customer")
        subscription_id = sub.get("id")
        status = sub.get("status", "canceled")

        # Try to find subscription by stripe_subscription_id
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id FROM subscriptions WHERE stripe_subscription_id = ?",
            (subscription_id,),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            upsert_subscription_from_stripe(
                user_id=row["user_id"],
                plan="free",
                status=status,
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
                current_period_end=None,
            )

    return JSONResponse({"received": True})


# -----------------------------------------------------------------------------
# ADMIN: subscribers + usage
# -----------------------------------------------------------------------------

@app.get("/admin/subscribers", response_model=AdminSubscribersResponse)
async def admin_subscribers(owner: dict = Depends(owner_dependency)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
          u.id as user_id,
          u.email,
          u.created_at,
          s.plan,
          s.status,
          s.current_period_end
        FROM users u
        LEFT JOIN subscriptions s ON s.user_id = u.id
        ORDER BY u.created_at DESC
        """
    )
    rows = cur.fetchall()
    conn.close()

    data = []
    for row in rows:
        data.append(
            {
                "user_id": row["user_id"],
                "email": row["email"],
                "created_at": row["created_at"],
                "plan": row["plan"] or "free",
                "status": row["status"] or "none",
                "current_period_end": row["current_period_end"],
            }
        )
    return AdminSubscribersResponse(data=data)


@app.get("/admin/usage", response_model=AdminUsageResponse)
async def admin_usage(owner: dict = Depends(owner_dependency)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*) as c FROM usage_events
        WHERE event_type = 'generate'
        """
    )
    row = cur.fetchone()
    total = row["c"] if row else 0

    cur.execute(
        """
        SELECT event_type, COUNT(*) as c
        FROM usage_events
        GROUP BY event_type
        ORDER BY c DESC
        """
    )
    rows = cur.fetchall()
    conn.close()

    events = [{"event_type": r["event_type"], "count": r["c"]} for r in rows]
    return AdminUsageResponse(total_generations=total, events=events)

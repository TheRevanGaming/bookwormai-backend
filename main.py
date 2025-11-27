import os
import sqlite3
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from openai import OpenAI

# ---------------------------
# Environment & OpenAI client
# ---------------------------

BASE_DIR = Path(__file__).resolve().parent

# Load .env file if present
load_dotenv(dotenv_path=BASE_DIR / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError(
        "OPENAI_API_KEY is not set. "
        "Edit your .env file in ~/bookworm and add:\n"
        'OPENAI_API_KEY="sk-...."\n'
    )

client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------------------
# System prompt
# ---------------------------

SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "system_prompt.txt"
if not SYSTEM_PROMPT_PATH.exists():
    raise RuntimeError(f"System prompt file not found at {SYSTEM_PROMPT_PATH}")

SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

# ---------------------------
# Database helpers
# ---------------------------

DB_PATH = BASE_DIR / "bookworm.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------
# Subscription / Owner logic
# ---------------------------

# Book Worm environment: "local" (default) or "prod"
BOOKWORM_ENV = os.getenv("BOOKWORM_ENV", "local")


def is_owner(request: Request) -> bool:
    """
    For now, in local mode you are ALWAYS treated as the owner.
    In the future (prod deployment), you can change this to check headers, cookies, etc.
    """
    if BOOKWORM_ENV == "local":
        return True

    # Example future logic (kept for extension):
    owner_key_env = os.getenv("BOOKWORM_OWNER_KEY")
    owner_key_req = request.headers.get("x-bookworm-owner-key")
    if owner_key_env and owner_key_req and owner_key_req == owner_key_env:
        return True

    return False


def get_request_plan(request: Request) -> str:
    """
    Very simple plan extraction from headers.
    Front-end can send: X-Bookworm-Plan: basic|pro|patron
    """
    return request.headers.get("x-bookworm-plan", "none").lower()


def enforce_subscription(request: Request) -> None:
    """
    Keep subscription scaffolding, but:
    - In local mode OR if owner => no enforcement.
    - In prod (future) => require a plan.
    """
    if is_owner(request):
        # You are always owner in local env, so you never get blocked.
        return

    plan = get_request_plan(request)
    if plan in ("none", "", "free"):
        # HTTP 402 = Payment Required
        raise HTTPException(
            status_code=402,
            detail="Subscription required. Please choose a plan to continue.",
        )


# ---------------------------
# Pydantic models
# ---------------------------


class GenerateRequest(BaseModel):
    prompt: str
    mode: str = "auto"  # 'auto', 'lore', 'world', 'character', etc.
    depth: str = "deep"  # 'deep' or 'super_deep'
    project_id: Optional[int] = 1  # default main project
    domain: Optional[str] = None  # GAME_DEV / MUSIC_DEV / STORYTELLING, if front-end wants to send


class GenerateResponse(BaseModel):
    response: str


class ImageRequest(BaseModel):
    prompt: str
    size: str = "1024x1024"  # "1024x1024", "1024x1792", "1792x1024"
    quality: str = "high"  # OpenAI: "low", "medium", "high", or "auto"
    n: int = 1


class ImageResponse(BaseModel):
    urls: List[str]


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


# ---------------------------
# Mode inference
# ---------------------------

def infer_mode_from_prompt(prompt: str) -> str:
    """Very simple heuristic to guess what the user wants."""
    text = prompt.lower()

    # Music / lyrics
    if "lyrics" in text or "chorus" in text or "verse" in text or "hook" in text:
        return "lyrics"
    if "beat" in text or "808" in text or "instrumental" in text or "ost" in text:
        return "instrumental_concept"
    if (
        "sound effect" in text
        or "sfx" in text
        or "ambience" in text
        or "audio design" in text
    ):
        return "audio_concept"

    # Characters
    if "character sheet" in text or "character profile" in text:
        return "character"
    if "backstory" in text or "origin story" in text:
        return "character"
    if "build me a character" in text or "design a character" in text:
        return "character"

    # Worlds / realms / biomes / flora-fauna
    if (
        "realm" in text
        or "continent" in text
        or "planet" in text
        or "world map" in text
    ):
        return "world"
    if "biome" in text or "region" in text or "climate" in text:
        return "world"
    if (
        "flora" in text
        or "fauna" in text
        or "creature" in text
        or "monster" in text
        or "species" in text
    ):
        return "world"
    if "culture" in text or "tribe" in text or "kingdom" in text or "empire" in text:
        return "world"
    if "magic system" in text or "power system" in text or "tech system" in text:
        return "world"

    # Lore / canon
    if "lore" in text or "canon" in text or "timeline" in text:
        return "lore"
    if "summarize my world" in text or "explain my world" in text:
        return "lore"

    # Scenes / blocks
    if "rewrite this" in text or "fix this paragraph" in text or "edit this scene" in text:
        return "block"
    if "write a scene" in text or "write a chapter" in text:
        return "block"

    # Default fallback
    return "free"


# ---------------------------
# FastAPI app setup
# ---------------------------

app = FastAPI(title="Book Worm AI", version="0.1.0")

# CORS (open, since this is local)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # you can tighten this later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files (frontend)
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    # Fallback minimal page
    return HTMLResponse(
        "<html><body><h1>Book Worm API</h1><p>static/index.html not found.</p></body></html>"
    )


# ---------------------------
# Projects & Docs endpoints
# ---------------------------

@app.post("/projects", response_model=Project)
def create_project(payload: ProjectCreate):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO projects (name, description) VALUES (?, ?)",
        (payload.name, payload.description),
    )
    project_id = cur.lastrowid
    conn.commit()
    conn.close()
    return Project(id=project_id, name=payload.name, description=payload.description)


@app.get("/projects", response_model=List[Project])
def list_projects():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, description FROM projects ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()
    return [
        Project(id=row["id"], name=row["name"], description=row["description"])
        for row in rows
    ]


@app.post("/docs", response_model=Doc)
def create_doc(payload: DocCreate):
    tags_str = ",".join(payload.tags) if payload.tags else None

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO docs (project_id, title, body, tags, canon_state, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            payload.project_id,
            payload.title,
            payload.body,
            tags_str,
            payload.canon_state,
            payload.source,
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
def list_docs(project_id: Optional[int] = None):
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


# ---------------------------
# Image generation endpoint
# ---------------------------

@app.post("/generate_image", response_model=ImageResponse)
async def generate_image(req: ImageRequest, request: Request):
    """
    Generate high-quality concept art or character/location images.
    Uses GPT Image with quality values that OpenAI currently supports.
    Any errors are returned as a string URL starting with 'ERROR:'.
    """
    # Enforce subscription only if not owner & not local (future)
    try:
        enforce_subscription(request)
    except HTTPException as e:
        # Don't crash the UI; surface error as pseudo-URL
        return ImageResponse(urls=[f"Image error: {e.detail}"])

    try:
        result = client.images.generate(
            model="gpt-image-1",
            prompt=req.prompt,
            size=req.size,
            n=req.n,
            quality=req.quality,  # must be "low","medium","high","auto"
        )
        urls: List[str] = []
        for d in result.data:
            # Some SDK versions use d.url; if None, just stringify
            url = getattr(d, "url", None)
            if url is None:
                url = "ERROR: image data returned without URL"
            urls.append(url)
        if not urls:
            urls = ["ERROR: no image URLs returned"]
        return ImageResponse(urls=urls)
    except Exception as e:
        # Avoid breaking the front-end; return error string instead
        return ImageResponse(urls=[f"Image error: {e}"])


# ---------------------------
# Core chat generation endpoint
# ---------------------------

@app.post("/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest, request: Request):
    """
    Core generation endpoint.

    - prompt: what the user wants
    - mode: 'auto','lore','world','character','block','free','lyrics',
            'instrumental_concept','audio_concept'
    - depth: 'deep' or 'super_deep'
    - project_id: which canon project to pull from (default 1)
    - domain: optional high-level domain (GAME_DEV, MUSIC_DEV, STORYTELLING)
    """
    # Subscription enforcement (no-op for you as owner in local env)
    try:
        enforce_subscription(request)
    except HTTPException as e:
        # Return as normal message so UI doesn't show "Backend error"
        return GenerateResponse(
            response=(
                "⚠ Subscription error in Book Worm.\n\n"
                f"Details: {e.detail}"
            )
        )

    # Resolve mode if 'auto'
    if req.mode == "auto":
        resolved_mode = infer_mode_from_prompt(req.prompt)
    else:
        resolved_mode = req.mode

    # Optional domain/tag, e.g. [DOMAIN: GAME_DEV]
    domain_prefix = ""
    if req.domain:
        domain_prefix = f"[DOMAIN: {req.domain}]\n\n"

    mode_line = f"Mode: {resolved_mode}"
    depth_line = f"Depth: {req.depth}"

    user_content = (
        f"{domain_prefix}{mode_line}\n{depth_line}\n\nUser prompt:\n{req.prompt}"
    )

    # Pull some canon for this project, if specified
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
            LIMIT 10
            """,
            (req.project_id,),
        )
        rows = cur.fetchall()
        conn.close()

        parts: List[str] = []
        for row in rows:
            parts.append(f"# {row['title']}\n{row['body']}")
        canon_text = "\n\n".join(parts)

        # Soft limit to keep context manageable
        max_chars = 8000
        if len(canon_text) > max_chars:
            canon_text = canon_text[:max_chars] + "\n\n...[canon truncated]..."

    try:
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

        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.8,
        )
        answer = completion.choices[0].message.content or ""
        return GenerateResponse(response=answer)
    except Exception as e:
        # Don't crash with 500; show a friendly error in the chat window
        return GenerateResponse(
            response=(
                "⚠ Book Worm hit an error talking to OpenAI.\n\n"
                f"Error type: {e.__class__.__name__}\n"
                f"Details: {e}"
            )
        )

import os
import json
import sqlite3
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel
from openai import OpenAI

# -----------------------------------------------------------------------------
# Environment & OpenAI client
# -----------------------------------------------------------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set in environment.")

client = OpenAI(api_key=OPENAI_API_KEY)

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "bookworm.db"
SCHEMA_PATH = BASE_DIR / "db" / "schema.sql"
SYSTEM_PROMPT_PATH = BASE_DIR / "prompts" / "system_prompt.txt"

BOOKWORM_OWNER_CODE = os.getenv("BOOKWORM_OWNER_CODE", "").strip()

# -----------------------------------------------------------------------------
# FastAPI app & CORS
# -----------------------------------------------------------------------------

app = FastAPI(title="Book Worm AI Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # adjust later if you want stricter
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# Static files (frontend)
# -----------------------------------------------------------------------------

static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def serve_index() -> HTMLResponse:
    """
    Serve the main Book Worm UI.
    """
    index_path = static_dir / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>Book Worm Backend is running.</h1>", status_code=200)
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


# -----------------------------------------------------------------------------
# DB helpers
# -----------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Apply schema.sql on startup (idempotent).
    """
    if not SCHEMA_PATH.exists():
        print("[init_db] WARNING: db/schema.sql not found. Skipping.")
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


def log_event(
    event_type: str,
    user_id: Optional[str] = None,
    user_tier: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Insert a row into events table for analytics.
    """
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO events (event_type, user_id, user_tier, metadata)
            VALUES (?, ?, ?, ?)
            """,
            (
                event_type,
                user_id,
                user_tier,
                json.dumps(metadata or {}),
            ),
        )
        conn.commit()
    except Exception as e:
        print(f"[log_event] Failed to log event: {e}")
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# System prompt
# -----------------------------------------------------------------------------

SYSTEM_PROMPT = "You are Book Worm AI."
if SYSTEM_PROMPT_PATH.exists():
    SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    print("[startup] System prompt loaded.")
else:
    print("[startup] WARNING: prompts/system_prompt.txt not found. Using fallback prompt.")


# -----------------------------------------------------------------------------
# Pydantic models
# -----------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    prompt: str
    mode: str = "auto"   # 'auto','lore','world','character','block','free', etc.
    depth: str = "deep"  # 'deep' or 'super_deep'
    project_id: Optional[int] = None

    # For analytics / admin
    user_id: Optional[str] = None
    user_tier: Optional[str] = None  # 'free','basic','pro','patron','owner', etc.

    class Config:
        extra = "ignore"  # ignore any unknown fields from the UI


class GenerateResponse(BaseModel):
    response: str


class ImageRequest(BaseModel):
    prompt: str
    size: str = "1024x1024"   # "1024x1024", "1024x1792", or "1792x1024"
    quality: str = "high"     # "low", "medium", "high", or "auto"
    n: int = 1                # number of images

    class Config:
        extra = "ignore"


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


class AnalyticsSummary(BaseModel):
    total_events: int
    by_type: Dict[str, int]
    last_24h_events: int
    total_generates: int
    total_image_generates: int


class RecentEvent(BaseModel):
    id: int
    created_at: str
    event_type: str
    user_id: Optional[str]
    user_tier: Optional[str]
    metadata: Dict[str, Any]


# -----------------------------------------------------------------------------
# Startup
# -----------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup() -> None:
    init_db()


# -----------------------------------------------------------------------------
# Canon helpers
# -----------------------------------------------------------------------------

def load_canon_for_project(project_id: int, soft_char_limit: int = 4000) -> str:
    """
    Load recent docs for the project and build a canon text block.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT title, body
            FROM docs
            WHERE project_id = ?
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    parts: List[str] = []
    for row in rows:
        parts.append(f"# {row['title']}\n{row['body']}")

    canon_text = "\n\n".join(parts)
    if len(canon_text) > soft_char_limit:
        canon_text = canon_text[:soft_char_limit] + "\n\n...[canon truncated]..."

    return canon_text


def infer_mode_from_prompt(prompt: str) -> str:
    """
    Very simple heuristic to guess what the user wants.
    """
    text = prompt.lower()

    # Music / lyrics
    if any(k in text for k in ["lyrics", "chorus", "verse", "hook"]):
        return "lyrics"
    if any(k in text for k in ["beat", "808", "instrumental", "ost"]):
        return "instrumental_concept"
    if any(k in text for k in ["sound effect", "sfx", "ambience", "audio design"]):
        return "audio_concept"

    # Characters
    if any(k in text for k in ["character sheet", "character profile"]):
        return "character"
    if any(k in text for k in ["backstory", "origin story"]):
        return "character"
    if any(k in text for k in ["build me a character", "design a character"]):
        return "character"

    # Worlds / realms / biomes / flora/fauna / culture
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

    # Lore / canon
    if any(k in text for k in ["lore", "canon", "timeline"]):
        return "lore"
    if any(k in text for k in ["summarize my world", "explain my world"]):
        return "lore"

    # Scenes / blocks
    if any(k in text for k in ["rewrite this", "fix this paragraph", "edit this scene"]):
        return "block"
    if any(k in text for k in ["write a scene", "write a chapter"]):
        return "block"

    # Default fallback
    return "free"


# -----------------------------------------------------------------------------
# Core generation endpoint (uses canon + logs analytics)
# -----------------------------------------------------------------------------

@app.post("/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest) -> GenerateResponse:
    """
    Core generation endpoint.

    - prompt: what the user wants
    - mode: 'auto','lore','world','character','block','free','lyrics',
            'instrumental_concept','audio_concept'
    - depth: 'deep' or 'super_deep'
    - project_id: optional, for canon-based work
    """
    resolved_mode = req.mode
    if req.mode == "auto":
        resolved_mode = infer_mode_from_prompt(req.prompt)

    canon_text = ""
    if req.project_id is not None:
        try:
            canon_text = load_canon_for_project(req.project_id)
        except Exception as e:
            print(f"[generate] Error loading canon for project {req.project_id}: {e}")
            canon_text = ""

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
            model="gpt-4.1-mini",
            messages=messages,
            temperature=0.8,
        )
        answer = completion.choices[0].message.content or ""

        # Log success event
        log_event(
            event_type="GENERATE",
            user_id=req.user_id,
            user_tier=req.user_tier,
            metadata={
                "mode": resolved_mode,
                "depth": req.depth,
                "project_id": req.project_id,
            },
        )

        return GenerateResponse(response=answer)
    except Exception as e:
        # Log error event
        log_event(
            event_type="ERROR",
            user_id=req.user_id,
            user_tier=req.user_tier,
            metadata={
                "where": "generate",
                "error_type": e.__class__.__name__,
                "error_message": str(e),
            },
        )
        return GenerateResponse(
            response=(
                "⚠ Book Worm hit an error talking to OpenAI.\n\n"
                f"Error type: {e.__class__.__name__}\n"
                f"Details: {e}"
            )
        )


# -----------------------------------------------------------------------------
# Image generation endpoint + analytics
# -----------------------------------------------------------------------------

@app.post("/generate_image", response_model=ImageResponse)
async def generate_image(req: ImageRequest) -> ImageResponse:
    """
    Generate concept art or character/location images using gpt-image-1.
    """
    try:
        result = client.images.generate(
            model="gpt-image-1",
            prompt=req.prompt,
            size=req.size,
            n=req.n,
            quality=req.quality,
        )
        urls: List[str] = []
        for d in result.data:
            # Some clients return .url, some encode differently; guard for safety
            url = getattr(d, "url", None)
            if isinstance(url, str):
                urls.append(url)

        if not urls:
            # If nothing usable came back, surface a clear error
            log_event(
                event_type="ERROR",
                user_id=None,
                user_tier=None,
                metadata={
                    "where": "generate_image",
                    "error_type": "NoImageURLs",
                    "error_message": "No valid image URLs returned by API.",
                },
            )
            return ImageResponse(urls=["ERROR: No valid image URLs returned by API."])

        # Log success event
        log_event(
            event_type="GENERATE_IMAGE",
            user_id=None,
            user_tier=None,
            metadata={
                "size": req.size,
                "quality": req.quality,
                "n": req.n,
            },
        )

        return ImageResponse(urls=urls)
    except Exception as e:
        log_event(
            event_type="ERROR",
            user_id=None,
            user_tier=None,
            metadata={
                "where": "generate_image",
                "error_type": e.__class__.__name__,
                "error_message": str(e),
            },
        )
        return ImageResponse(
            urls=[
                f"Image error: {e.__class__.__name__}: {e}"
            ]
        )


# -----------------------------------------------------------------------------
# Projects & Docs (canon API) -- already gives you "cloud sync"
# -----------------------------------------------------------------------------

@app.post("/projects", response_model=Project)
def create_project(payload: ProjectCreate) -> Project:
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO projects (name, description) VALUES (?, ?)",
            (payload.name, payload.description),
        )
        project_id = cur.lastrowid
        conn.commit()
        return Project(id=project_id, name=payload.name, description=payload.description)
    finally:
        conn.close()


@app.get("/projects", response_model=List[Project])
def list_projects() -> List[Project]:
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, description FROM projects ORDER BY created_at DESC"
        )
        rows = cur.fetchall()
        return [
            Project(
                id=row["id"],
                name=row["name"],
                description=row["description"],
            )
            for row in rows
        ]
    finally:
        conn.close()


@app.post("/docs", response_model=Doc)
def create_doc(payload: DocCreate) -> Doc:
    tags_str = ",".join(payload.tags) if payload.tags else None

    conn = get_db()
    try:
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
        return Doc(
            id=doc_id,
            project_id=payload.project_id,
            title=payload.title,
            body=payload.body,
            tags=payload.tags,
            canon_state=payload.canon_state,
            source=payload.source,
        )
    finally:
        conn.close()


@app.get("/docs", response_model=List[Doc])
def list_docs(project_id: Optional[int] = None) -> List[Doc]:
    conn = get_db()
    try:
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
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Admin Analytics Endpoints (JSON dashboard)
# -----------------------------------------------------------------------------

def _is_owner(request: Request) -> bool:
    """
    Very simple owner check:
    - If BOOKWORM_OWNER_CODE is empty => no owner security
    - Else, require header X-Bookworm-Owner-Code to match
    """
    if not BOOKWORM_OWNER_CODE:
        # No owner code set; treat as open for now
        return True
    header_code = request.headers.get("X-Bookworm-Owner-Code", "")
    return header_code.strip() == BOOKWORM_OWNER_CODE


@app.get("/admin/stats/summary", response_model=AnalyticsSummary)
async def admin_stats_summary(request: Request) -> AnalyticsSummary:
    if not _is_owner(request):
        raise HTTPException(status_code=403, detail="Not authorized (owner only).")

    conn = get_db()
    try:
        cur = conn.cursor()

        # total events
        cur.execute("SELECT COUNT(*) AS c FROM events")
        total_events = cur.fetchone()["c"]

        # events per type
        cur.execute(
            """
            SELECT event_type, COUNT(*) AS c
            FROM events
            GROUP BY event_type
            """
        )
        by_type_rows = cur.fetchall()
        by_type = {row["event_type"]: row["c"] for row in by_type_rows}

        # last 24h
        cur.execute(
            """
            SELECT COUNT(*) AS c
            FROM events
            WHERE datetime(created_at) >= datetime('now', '-1 day')
            """
        )
        last_24h_events = cur.fetchone()["c"]

        total_generates = by_type.get("GENERATE", 0)
        total_image_generates = by_type.get("GENERATE_IMAGE", 0)

        return AnalyticsSummary(
            total_events=total_events,
            by_type=by_type,
            last_24h_events=last_24h_events,
            total_generates=total_generates,
            total_image_generates=total_image_generates,
        )
    finally:
        conn.close()


@app.get("/admin/events/recent", response_model=List[RecentEvent])
async def admin_events_recent(
    request: Request,
    limit: int = 50,
) -> List[RecentEvent]:
    if not _is_owner(request):
        raise HTTPException(status_code=403, detail="Not authorized (owner only).")

    if limit <= 0:
        limit = 1
    if limit > 200:
        limit = 200

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, created_at, event_type, user_id, user_tier, metadata
            FROM events
            ORDER BY datetime(created_at) DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
        out: List[RecentEvent] = []
        for row in rows:
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except json.JSONDecodeError:
                meta = {}
            out.append(
                RecentEvent(
                    id=row["id"],
                    created_at=row["created_at"],
                    event_type=row["event_type"],
                    user_id=row["user_id"],
                    user_tier=row["user_tier"],
                    metadata=meta,
                )
            )
        return out
    finally:
        conn.close()

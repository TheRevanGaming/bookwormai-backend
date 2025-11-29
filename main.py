import os
import json
import sqlite3
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

# -----------------------------
# ENV + OPENAI CLIENT
# -----------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set in environment.")

client = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------
# SYSTEM PROMPT
# -----------------------------

SYSTEM_PROMPT_PATH = Path("prompts/system_prompt.txt")
if SYSTEM_PROMPT_PATH.exists():
    SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
else:
    SYSTEM_PROMPT = """
You are Book Worm AI — an advanced AAA-quality assistant for:

- Storytelling & book writing
- AAA game development
- Music & lyrics
- Language creation
- Concept art prompts

Obey LOCKED CANON when provided. Infer the active domain from tags like:
[DOMAIN: STORYTELLING], [DOMAIN: GAME_DEV], [DOMAIN: MUSIC_DEV],
[DOMAIN: BOOK], [DOMAIN: LANGUAGE_LAB], [DOMAIN: CODING].

Respond clearly, structurally, and with AAA-level detail.
"""

# -----------------------------
# DATABASE SETUP
# -----------------------------

DB_PATH = Path("bookworm.db")
SCHEMA_PATH = Path("db/schema.sql")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Initialize DB from db/schema.sql if present.
    If the file or tables already exist, fail silently.
    """
    if not SCHEMA_PATH.exists():
        # No schema file; just return. We'll handle missing tables at query time.
        return

    try:
        conn = get_db()
        with SCHEMA_PATH.open("r", encoding="utf-8") as f:
            schema_sql = f.read()
        conn.executescript(schema_sql)
        conn.commit()
    except Exception as e:
        # Don't crash the app if schema fails; just print so we can debug.
        print(f"[init_db] Error applying schema: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def table_exists(table_name: str) -> bool:
    """Check if a table exists in SQLite."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        row = cur.fetchone()
        return row is not None
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


# -----------------------------
# Pydantic MODELS
# -----------------------------

class GenerateRequest(BaseModel):
    prompt: str
    mode: str = "auto"   # 'auto','lore','world','character','block','free','lyrics',...
    depth: str = "deep"  # 'deep','super_deep'
    project_id: Optional[int] = 1  # default to main project


class GenerateResponse(BaseModel):
    response: str


class ImageRequest(BaseModel):
    prompt: str
    size: str = "1024x1024"        # or "1024x1792", "1792x1024"
    quality: str = "high"          # 'low','medium','high','auto'
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


# -----------------------------
# MODE INFERENCE
# -----------------------------

def infer_mode_from_prompt(prompt: str) -> str:
    """Simple heuristic to guess what the user wants."""
    text = prompt.lower()

    # Music / lyrics
    if any(w in text for w in ["lyrics", "chorus", "verse", "hook"]):
        return "lyrics"
    if any(w in text for w in ["beat", "808", "instrumental", "ost"]):
        return "instrumental_concept"
    if any(w in text for w in ["sound effect", "sfx", "ambience", "audio design"]):
        return "audio_concept"

    # Characters
    if any(w in text for w in ["character sheet", "character profile"]):
        return "character"
    if any(w in text for w in ["backstory", "origin story"]):
        return "character"
    if any(w in text for w in ["build me a character", "design a character"]):
        return "character"

    # Worlds / realms / flora-fauna
    if any(w in text for w in ["realm", "continent", "planet", "world map"]):
        return "world"
    if any(w in text for w in ["biome", "region", "climate"]):
        return "world"
    if any(w in text for w in ["flora", "fauna", "creature", "monster", "species"]):
        return "world"
    if any(w in text for w in ["culture", "tribe", "kingdom", "empire"]):
        return "world"
    if any(w in text for w in ["magic system", "power system", "tech system"]):
        return "world"

    # Lore / canon
    if any(w in text for w in ["lore", "canon", "timeline"]):
        return "lore"
    if any(w in text for w in ["summarize my world", "explain my world"]):
        return "lore"

    # Scenes / blocks
    if any(w in text for w in ["rewrite this", "fix this paragraph", "edit this scene"]):
        return "block"
    if any(w in text for w in ["write a scene", "write a chapter"]):
        return "block"

    # Default fallback
    return "free"


# -----------------------------
# FASTAPI APP
# -----------------------------

app = FastAPI(title="Book Worm AI Backend", version="0.3.0")

# CORS – allow your site + local dev
origins = [
    "http://localhost",
    "http://localhost:5050",
    "http://127.0.0.1:5050",
    # add your Render frontend or GitHub Pages URL here if needed
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins + ["*"],  # you can tighten this later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
static_dir = Path("static")
if static_dir.exists():
    app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
def on_startup() -> None:
    """Initialize DB on startup (Render & local)."""
    init_db()


# -----------------------------
# ROUTES
# -----------------------------

@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    """
    Serve the SPA (index.html).
    """
    index_path = static_dir / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Book Worm AI backend is running.</h1>", status_code=200)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------- Projects / Docs (Canon) ----------

@app.post("/projects", response_model=Project)
def create_project(payload: ProjectCreate):
    if not table_exists("projects"):
        raise HTTPException(status_code=500, detail="Projects table not initialized.")
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
    if not table_exists("projects"):
        # No projects yet
        return []
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
    if not table_exists("docs"):
        raise HTTPException(status_code=500, detail="Docs table not initialized.")
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
    if not table_exists("docs"):
        return []
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


# ---------- Core Chat / Generate ----------

@app.post("/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest):
    """
    Core generation endpoint.

    - prompt: what the user wants
    - mode: 'auto','lore','world','character','block','free',
            'lyrics','instrumental_concept','audio_concept'
    - depth: 'deep' or 'super_deep'
    """
    # Resolve mode if 'auto'
    if req.mode == "auto":
        resolved_mode = infer_mode_from_prompt(req.prompt)
    else:
        resolved_mode = req.mode

    # Pull canon docs for this project, if the docs table exists
    canon_text = ""
    if req.project_id is not None and table_exists("docs"):
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT title, body
                FROM docs
                WHERE project_id = ?
                ORDER BY created_at DESC
                LIMIT 5
                """,
                (req.project_id,),
            )
            rows = cur.fetchall()
            conn.close()
            parts = []
            for row in rows:
                parts.append(f"# {row['title']}\n{row['body']}")
            canon_text = "\n\n".join(parts)
            if len(canon_text) > 4000:
                canon_text = canon_text[:4000] + "\n\n...[canon truncated]..."
        except Exception as e:
            print(f"[generate] Error loading canon: {e}")
            canon_text = ""

    mode_line = f"Mode: {resolved_mode}"
    depth_line = f"Depth: {req.depth}"

    user_content = f"{mode_line}\n{depth_line}\n\nUser prompt:\n{req.prompt}"

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
        # Return a friendly error instead of 500
        return GenerateResponse(
            response=(
                "⚠ Book Worm hit an error talking to OpenAI.\n\n"
                f"Error type: {e.__class__.__name__}\n"
                f"Details: {e}"
            )
        )


# ---------- Image Generation ----------

@app.post("/generate_image", response_model=ImageResponse)
async def generate_image(req: ImageRequest):
    """
    Generate high-quality concept art or character/location images.
    Uses GPT Image (or your configured image model) via OpenAI.
    """
    try:
        result = client.images.generate(
            model="gpt-image-1",
            prompt=req.prompt,
            size=req.size,
            n=req.n,
            quality=req.quality,  # 'low','medium','high','auto'
        )
        urls: List[str] = []
        for d in result.data:
            # some clients use d.url, others d["url"]
            url = getattr(d, "url", None)
            if not url and isinstance(d, dict):
                url = d.get("url")
            if url:
                urls.append(url)
        if not urls:
            urls = ["ERROR: No URL returned from image API."]
        return ImageResponse(urls=urls)
    except Exception as e:
        return ImageResponse(urls=[f"ERROR: Image error: {e}"])

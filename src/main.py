from pathlib import Path
from html import escape
import json
import logging
import re
import shutil
from typing_extensions import Annotated
import uuid
from urllib.parse import quote
from datetime import datetime, time, timedelta, timezone
from fastapi import FastAPI, File, Form, Header, Request, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn
import os
import time
from dotenv import load_dotenv
from pyngrok import ngrok
import psutil
import random
import secrets
import string
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

load_dotenv()

start_time = time.time()


ph = PasswordHasher()

def hash_password(password: str) -> str:
    return ph.hash(password)

def configure_logging() -> logging.Logger:
    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    root_logger = logging.getLogger()

    if not root_logger.handlers:
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )

    logger = logging.getLogger("ghostdrop")
    logger.setLevel(log_level)
    return logger


logger = configure_logging()

EXPIRY_HOURS = 6 # 6 hours 
EXPIRY_DURATION = timedelta(hours=EXPIRY_HOURS)
EXPIRED_MESSAGE = "this file is gone"
SRC_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path("uploads")
METADATA_DIR = Path("uploads_meta")
DOWNLOAD_TEMPLATE_PATH = SRC_DIR / "public" / "download.html"
DOWNLOAD_STYLES_PATH = SRC_DIR / "public" / "download.css"
MAX_SIZE = 100 * 1024 * 1024  # 100MB
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
RESERVED_SLUGS = {
    "upload", "download", "delete", "health", "files",
    "metadata", "api", "admin", "static", "assets",
    "public", "docs", "openapi", "redoc", "www",
    "root", "system", "config", "help", "status", "upload", "files", "metadata", "delete",
}
#fucking needs to be an integer
service_port = int(os.getenv("PORT"))
ngrok_status = os.getenv("NGROK_STATUS", "false").lower() == "true"
def start_ngrok_tunnel():
    if ngrok_status == True:
        ngrok.set_auth_token(os.getenv("NGROK_TOKEN"))
        tunnel = ngrok.connect(service_port, "http")
        logger.info("Ngrok tunnel established at %s", tunnel.public_url)
    else:
        logger.info("Ngrok tunneling is disabled. Running on port %s", service_port)
        print("server is running without ngrok tunneling")

start_ngrok_tunnel()

def metadata_path(file_id: str) -> Path:
    return METADATA_DIR / f"{file_id}.json"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def cleanup_file(file_id: str) -> None:
    file_path = UPLOAD_DIR / file_id
    metadata_file = metadata_path(file_id)

    if file_path.exists():
        file_path.unlink()
        logger.info("Deleted uploaded file %s", file_id)
    if metadata_file.exists():
        metadata_file.unlink()
        logger.info("Deleted metadata for %s", file_id)


def format_file_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "unknown size"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def format_expiry_label(expires_at: str | None) -> str:
    if not expires_at:
        return "unknown"

    try:
        expires_dt = datetime.fromisoformat(expires_at).astimezone(timezone.utc)
    except ValueError:
        return "unknown"

    remaining = expires_dt - utc_now()
    if remaining.total_seconds() <= 0:
        return "expired"

    total_minutes = int(remaining.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)

    if hours <= 0:
        return f"in {max(minutes, 1)}m"

    return f"in {hours}h {minutes}m"


def build_embed_description(original_name: str, size_bytes: int | None, views: int = 0) -> str:
    file_label = f"{original_name} ({format_file_size(size_bytes)})"
    return (
        f"{file_label} - {views} views - Download your file from GhostDrop - A secure, anonymous file sharing platform. "
        "This file was uploaded by a user. Download only if you trust the source."
    )


def write_metadata(
    file_id: str,
    original_name: str,
    size_bytes: int | None = None,
    password: str | None = None,
    password_hash: str | None = None,
) -> None:
    metadata = {
        "password_hash": password_hash,
        "has_password": password_hash is not None,
        "original_name": original_name,
        "size_bytes": size_bytes,
        "expires_at": (utc_now() + EXPIRY_DURATION).isoformat(),
        "views": 0,
    }
    metadata_path(file_id).write_text(json.dumps(metadata), encoding="utf-8")
    logger.info("Stored metadata for %s (%s)", file_id, original_name)


def load_metadata(file_id: str) -> dict | None:
    path = metadata_path(file_id)
    if not path.exists():
        return None
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata.setdefault("views", 0)
    return metadata


def save_metadata(file_id: str, metadata: dict) -> None:
    metadata_path(file_id).write_text(json.dumps(metadata), encoding="utf-8")


def increment_views(file_id: str, metadata: dict) -> dict:
    metadata["views"] = int(metadata.get("views", 0)) + 1
    save_metadata(file_id, metadata)
    logger.info("View count for %s is now %s", file_id, metadata["views"])
    return metadata


def is_expired(metadata: dict) -> bool:
    return utc_now() >= datetime.fromisoformat(metadata["expires_at"])


def generate_file_id(slug: str | None = None, length: int = 6) -> str:
    if slug and isinstance(slug, str) and SLUG_PATTERN.fullmatch(slug):
        return slug

    if slug:
        logger.warning("Invalid slug provided, generating random file id instead: %s", slug)

    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def cleanup_expired_files() -> None:
    deleted_files = 0
    for path in METADATA_DIR.glob("*.json"):
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if is_expired(metadata):
            cleanup_file(path.stem)
            deleted_files += 1

    if deleted_files:
        logger.info("Cleaned up %s expired file(s)", deleted_files)


def render_download_page(file_id: str, metadata: dict, file_path: Path) -> str:
    quoted_file_id = quote(file_id, safe="")
    size_bytes = metadata.get("size_bytes")
    views = metadata.get("views", 0)
    original_name = metadata.get("original_name", file_id)
    expires_at = metadata.get("expires_at")
    has_password = metadata.get("has_password", False)

    if size_bytes is None and file_path.exists():
        size_bytes = file_path.stat().st_size

    embed_description = escape(
        build_embed_description(
            original_name,
            size_bytes,
            views,
        )
    )

    return DOWNLOAD_TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "__FILE_ID_URL__",
        quoted_file_id,
    ).replace(
        "__FILE_ID__",
        escape(file_id),
    ).replace(
        "__FILE_NAME__",
        escape(original_name),
    ).replace(
        "__FILE_SIZE__",
        escape(format_file_size(size_bytes)),
    ).replace(
        "__FILE_VIEWS__",
        escape(str(views)),
    ).replace(
        "__FILE_EXPIRY__",
        escape(format_expiry_label(expires_at)),
    ).replace(
        "__FILE_ACCESS__",
        "password protected" if has_password else "open link",
    ).replace(
        "__EMBED_DESCRIPTION__",
        embed_description,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic (replaces @app.on_event("startup"))
    UPLOAD_DIR.mkdir(exist_ok=True)
    METADATA_DIR.mkdir(exist_ok=True)
    cleanup_expired_files()
    logger.info("Starting GhostDrop backend")
    yield
    # Shutdown logic (replaces @app.on_event("shutdown"))
    logger.info("Shutting down GhostDrop backend")


version = os.getenv("APP_VERSION")

app = FastAPI(
    title="ghostdrop",
    version=version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # tighten in production
    allow_methods=["*"],
    allow_headers=["*"],
)



@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = utc_now()
    status_code = 500

    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        duration_ms = (utc_now() - start_time).total_seconds() * 1000
        logger.info(
            "%s %s -> %s (%.2f ms)",
            request.method,
            request.url.path,
            status_code,
            duration_ms,
        )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    logger.warning(
        "HTTP %s on %s %s: %s",
        exc.status_code,
        request.method,
        request.url.path,
        exc.detail,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception(
        "Unhandled error during %s %s",
        request.method,
        request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"},
    )



@app.get("/")
async def index():
    return RedirectResponse(
    "https://ghostdrop.qzz.io",
    status_code=302
)

@app.get("/health")
async def health_check():
    
    cpu_usage = psutil.cpu_percent(interval=1)
    memory_usage = psutil.virtual_memory().percent
    uptime = round(time.time() - start_time, 2)

    return {
        "files_stored": len(list(UPLOAD_DIR.glob("*"))),
        "cpu_usage": f"{cpu_usage}%",
        "memory_usage": f"{memory_usage}%",
        "uptime": f"{uptime} seconds",
    }


@app.post("/upload/")
async def upload_file(file: UploadFile = File(...), Authorisation: Annotated[str | None, Form()] = None, slug: Annotated[str | None, Form()] = None):

    if file.size > MAX_SIZE:
        logger.warning("Rejected upload for %s: file too large", file.filename)
        raise HTTPException(status_code=413, detail="File too large")

    if slug:
        slug_lower = slug.lower()
        if not SLUG_PATTERN.fullmatch(slug):
            raise HTTPException(status_code=400, detail="Slug can only contain letters, numbers, hyphens and underscores")
        if slug_lower in RESERVED_SLUGS:
            raise HTTPException(status_code=400, detail="That slug is reserved and cannot be used")
        if len(slug) < 2:
            raise HTTPException(status_code=400, detail="Slug must be at least 2 characters long")

    cleanup_expired_files()

    file_id = generate_file_id(slug)
    file_path = UPLOAD_DIR / file_id

    if file_path.exists():
        raise HTTPException(status_code=409, detail="Slug already in use")

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    password_hash = None
    if Authorisation:
        password_hash = hash_password(Authorisation)

    write_metadata(
        file_id,
        file.filename or file_id,
        size_bytes=file_path.stat().st_size,
        password_hash=password_hash
    )    
    logger.info("Stored upload %s as %s", file.filename, file_id)

    return {
        "id": file_id,
        "original_name": file.filename,
        "expires_in_hours": EXPIRY_HOURS,
    }


@app.get("/download.css")
async def download_styles():
    return FileResponse(DOWNLOAD_STYLES_PATH)


@app.get("/{file_id}", response_class=HTMLResponse)
async def read_items(file_id: str):
    metadata = load_metadata(file_id)
    if metadata and is_expired(metadata):
        logger.info("Landing page requested for expired file %s", file_id)
        cleanup_file(file_id)
        return JSONResponse(
            status_code=410,
        )

    file_path = UPLOAD_DIR / file_id
    if not metadata or not file_path.exists():
        logger.warning("Landing page requested for missing file %s", file_id)
        raise HTTPException(status_code=404, detail="File not found")

    increment_views(file_id, metadata)
    logger.info("Rendering download page for %s", file_id)
    return HTMLResponse(content=render_download_page(file_id, metadata, file_path))

@app.get("/files/{file_id}")
async def get_file(file_id: str, Authorisation: Annotated[str | None, Header()] = None):
    metadata = load_metadata(file_id)
    if metadata and is_expired(metadata):
        logger.info("Download requested for expired file %s", file_id)
        cleanup_file(file_id)
        return JSONResponse(
            status_code=410,
            content={"error": EXPIRED_MESSAGE},
        )

    file_path = UPLOAD_DIR / file_id
    if not file_path.exists():
        logger.warning("Download requested for missing file %s", file_id)
        raise HTTPException(status_code=404, detail="File not found")

    if metadata and metadata.get("has_password"):
        password_hash = metadata.get("password_hash")

        if not Authorisation or not password_hash:
            logger.warning("Unauthorized download attempt for protected file %s", file_id)
            raise HTTPException(status_code=401, detail="Unauthorized")

        try:
            ph.verify(password_hash, Authorisation)
        except VerifyMismatchError:
            logger.warning("Unauthorized download attempt for protected file %s", file_id)
            raise HTTPException(status_code=401, detail="Unauthorized")

    filename = metadata["original_name"] if metadata else file_path.name
    logger.info("Serving file %s as %s", file_id, filename)
    return FileResponse(path=file_path, filename=filename)

@app.get("/metadata/{file_id}")
async def get_metadata(file_id: str):
    metadata = load_metadata(file_id)
    if not metadata:
        logger.warning("Metadata requested for missing file %s", file_id)
        raise HTTPException(status_code=404, detail="File not found")

    if is_expired(metadata):
        logger.info("Metadata requested for expired file %s", file_id)
        cleanup_file(file_id)
        return JSONResponse(
            status_code=410,
            content={"error": EXPIRED_MESSAGE},
        )

    logger.info("Metadata retrieved for %s", file_id)
    return {
        "original_name": metadata["original_name"],
        "size_bytes": metadata.get("size_bytes"),
        "expires_at": metadata["expires_at"],
        "views": metadata.get("views", 0),
        "has_password": metadata.get("has_password", False),
    }

@app.delete("/delete/{file_id}")
async def delete_file(file_id: str, password: Annotated[str | None, Header()] = None ):
    
    if password != os.getenv("DELETE_PASSWORD"):
        logger.warning("Unauthorized delete attempt for file %s", file_id)
        raise HTTPException(status_code=401, detail="Unauthorized")
    else:
        os.remove(UPLOAD_DIR / file_id)
        os.remove(metadata_path(file_id))
        logging.info("Deleted file %s via API", file_id)
        return {"detail": "File deleted"}

@app.exception_handler(404)
async def custom_404_handler(request: Request, exc):
    not_found_path = SRC_DIR / "public" / "404.html"
    return FileResponse(path=str(not_found_path), status_code=404)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=service_port, reload=False)

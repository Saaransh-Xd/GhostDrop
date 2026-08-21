from pathlib import Path
from html import escape
import base64
import hashlib
import hmac
import json
import logging
import re
import shutil
import tempfile
import zipfile
from typing_extensions import Annotated
import uuid
from urllib.parse import quote
from datetime import datetime, time, timedelta, timezone
from fastapi import FastAPI, File, Form, Header, Query, Request, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from starlette.background import BackgroundTask
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
import mimetypes

try:
    import boto3
    from botocore.config import Config as BotoConfig
except ImportError:  # Optional unless STORAGE_BACKEND=s3
    boto3 = None
    BotoConfig = None

load_dotenv()

start_time = time.time()


ph = PasswordHasher()

def hash_password(password: str) -> str:
    return ph.hash(password)

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

def create_folder_token(folder_id: str, ttl_seconds: int | None = None) -> str:
    if ttl_seconds is None:
        ttl_seconds = FOLDER_COOKIE_MAX_AGE
    payload = json.dumps({
        "fid": folder_id,
        "exp": utc_now().timestamp() + ttl_seconds,
    }).encode()
    signature = hmac.new(SERVER_SECRET, payload, hashlib.sha256).digest()
    return _b64url_encode(payload) + "." + _b64url_encode(signature)

def verify_folder_token(token: str | None, folder_id: str) -> bool:
    if not token or "." not in token:
        return False

    try:
        payload_b64, signature_b64 = token.rsplit(".", 1)
        payload = _b64url_decode(payload_b64)
        signature = _b64url_decode(signature_b64)
    except (ValueError, TypeError):
        return False

    expected = hmac.new(SERVER_SECRET, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return False

    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return False

    return data.get("fid") == folder_id and data.get("exp", 0) > utc_now().timestamp()

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
EXPIRY_LIMITS = {"hours": 12, "days": 6, "weeks": 3}
EXPIRED_MESSAGE = "this file is gone"
SRC_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path("uploads")
METADATA_DIR = Path("uploads_meta")
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local").strip().lower()


class ObjectStorage:
    """Small S3-compatible storage adapter with a local-filesystem fallback."""

    def __init__(self):
        self.remote = STORAGE_BACKEND in {"s3", "r2", "minio", "s3-compatible"}
        self.client = None
        self.bucket = os.getenv("S3_BUCKET", "").strip()
        self.prefix = os.getenv("S3_PREFIX", "ghostdrop").strip("/")

        if not self.remote:
            return
        if boto3 is None:
            raise RuntimeError("boto3 is required when STORAGE_BACKEND is s3-compatible")
        if not self.bucket:
            raise RuntimeError("S3_BUCKET is required when STORAGE_BACKEND is s3-compatible")

        self.client = boto3.client(
            "s3",
            endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
            region_name=os.getenv("S3_REGION", "auto"),
            aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID") or None,
            aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY") or None,
            aws_session_token=os.getenv("S3_SESSION_TOKEN") or None,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": os.getenv("S3_ADDRESSING_STYLE", "auto")},
            ),
        )
        logger.info("Using S3-compatible object storage (%s)", STORAGE_BACKEND)

    def key(self, file_id: str, child_id: str | None = None) -> str:
        parts = [part for part in (self.prefix, file_id, child_id) if part]
        return "/".join(parts)

    def put_file(self, fileobj, key: str, content_type: str | None = None) -> None:
        if not self.remote:
            return
        extra_args = {"ContentType": content_type} if content_type else {}
        self.client.upload_fileobj(fileobj, self.bucket, key, ExtraArgs=extra_args)

    def exists(self, key: str) -> bool:
        if not self.remote:
            return False
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except self.client.exceptions.ClientError:
            return False

    def download_to_temp(self, key: str) -> str:
        temp_file = tempfile.NamedTemporaryFile(delete=False)
        temp_path = temp_file.name
        temp_file.close()
        try:
            self.client.download_file(self.bucket, key, temp_path)
        except Exception:
            Path(temp_path).unlink(missing_ok=True)
            raise
        return temp_path

    def delete(self, key: str) -> None:
        if self.remote:
            self.client.delete_object(Bucket=self.bucket, Key=key)

    def delete_prefix(self, prefix: str) -> None:
        if not self.remote:
            return
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if objects:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": objects})


storage = ObjectStorage()
DOWNLOAD_TEMPLATE_PATH = SRC_DIR / "public" / "download.html"
DOWNLOAD_STYLES_PATH = SRC_DIR / "public" / "download.css"
FOLDER_TEMPLATE_PATH = SRC_DIR / "public" / "folder.html"
FOLDER_STYLES_PATH = SRC_DIR / "public" / "folder.css"
PASTE_TEMPLATE_PATH = SRC_DIR / "public" / "paste.html"
PASTE_STYLES_PATH = SRC_DIR / "public" / "paste.css"
FOLDER_ID_PREFIX = "f"
FOLDER_COOKIE = "ghostdrop_folder"
FOLDER_COOKIE_MAX_AGE = 6 * 3600
SERVER_SECRET = (os.getenv("GHOSTDROP_SECRET") or secrets.token_hex(32)).encode()
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

    if storage.remote:
        storage.delete(storage.key(file_id))
        storage.delete_prefix(storage.key(file_id) + "/")

    if file_path.is_dir():
        shutil.rmtree(file_path)
        logger.info("Deleted uploaded folder %s", file_id)
    elif file_path.exists():
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

def write_paste(paste_data: str, paste_id: str) -> Path:
    """Write a paste and return the path of the created file."""
    paste_dir = UPLOAD_DIR / "pastes"
    paste_dir.mkdir(parents=True, exist_ok=True)
    paste_file_path = paste_dir / f"{paste_id}.txt"

    # Exclusive creation prevents a collision from overwriting an existing paste.
    file_created = False
    try:
        with paste_file_path.open("x", encoding="utf-8") as paste_file:
            file_created = True
            paste_file.write(paste_data)
    except OSError:
        # Do not leave a partial paste behind if writing fails.
        if file_created:
            paste_file_path.unlink(missing_ok=True)
        raise

    return paste_file_path


def paste_metadata_path(paste_id: str) -> Path:
    return UPLOAD_DIR / "pastes" / f"{paste_id}.json"


def create_paste(
    paste_id: str,
    paste_data: str,
    password_hash: str | None = None,
) -> str:
    """Create a paste, store its access metadata, and return its id."""
    paste_file_path = write_paste(paste_data, paste_id)
    metadata_path = paste_metadata_path(paste_id)
    metadata_created = False
    try:
        with metadata_path.open("x", encoding="utf-8") as metadata_file:
            metadata_created = True
            json.dump({"password_hash": password_hash}, metadata_file)
    except OSError:
        paste_file_path.unlink(missing_ok=True)
        if metadata_created:
            metadata_path.unlink(missing_ok=True)
        raise

    logger.info("Stored paste %s", paste_id)
    return paste_id

def write_metadata(
    file_id: str,
    original_name: str,
    size_bytes: int | None = None,
    password: str | None = None,
    password_hash: str | None = None,
    is_static: bool = False,
    expiry_duration: timedelta = EXPIRY_DURATION,
) -> None:
    metadata = {
        "password_hash": password_hash,
        "has_password": password_hash is not None,
        "original_name": original_name,
        "size_bytes": size_bytes,
        "expires_at": (utc_now() + expiry_duration).isoformat(),
        "views": 0,
        "is_static": is_static
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


def id_in_use(file_id: str) -> bool:
    return (
        metadata_path(file_id).exists()
        or (UPLOAD_DIR / file_id).exists()
        or (UPLOAD_DIR / "pastes" / f"{file_id}.txt").exists()
        or paste_metadata_path(file_id).exists()
    )


def object_key(file_id: str, child_id: str | None = None) -> str:
    return storage.key(file_id, child_id)


def object_exists(file_id: str, child_id: str | None = None) -> bool:
    if storage.remote:
        return storage.exists(object_key(file_id, child_id))
    path = UPLOAD_DIR / file_id
    if child_id:
        path /= child_id
    return path.exists()


def materialize_object(file_id: str, child_id: str | None = None) -> tuple[Path, bool]:
    """Return a local path for a stored object and whether it must be cleaned up."""
    if storage.remote:
        return Path(storage.download_to_temp(object_key(file_id, child_id))), True
    path = UPLOAD_DIR / file_id
    if child_id:
        path /= child_id
    return path, False


def validate_slug_input(slug: str) -> None:
    slug_lower = slug.lower()
    if not SLUG_PATTERN.fullmatch(slug):
        raise HTTPException(status_code=400, detail="Slug can only contain letters, numbers, hyphens and underscores")
    if slug_lower in RESERVED_SLUGS:
        raise HTTPException(status_code=400, detail="That slug is reserved and cannot be used")
    if len(slug) < 2:
        raise HTTPException(status_code=400, detail="Slug must be at least 2 characters long")


def validate_duration(duration: int | None, duration_unit: str | None) -> int:
    duration_unit = (duration_unit or "hours").lower()
    max_duration = EXPIRY_LIMITS.get(duration_unit)
    if max_duration is None or duration is None or duration < 1 or duration > max_duration:
        limits = ", ".join(f"{unit}: 1-{limit}" for unit, limit in EXPIRY_LIMITS.items())
        raise HTTPException(status_code=400, detail=f"Duration must be between 1 and the maximum for its unit ({limits})")
    return duration * {"hours": 1, "days": 24, "weeks": 168}[duration_unit]


def generate_folder_id(slug: str | None = None) -> str:
    if slug:
        return slug

    for _ in range(20):
        candidate = FOLDER_ID_PREFIX + ''.join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(5)
        )
        if not id_in_use(candidate):
            return candidate

    raise HTTPException(status_code=500, detail="Failed to allocate folder id")


def is_folder_authorized(folder_id: str, metadata: dict, request: Request, password: str | None = None) -> bool:
    if not metadata.get("has_password"):
        return True

    if verify_folder_token(request.cookies.get(FOLDER_COOKIE), folder_id):
        return True

    password_hash = metadata.get("password_hash")
    if password and password_hash:
        try:
            ph.verify(password_hash, password)
            return True
        except VerifyMismatchError:
            return False

    return False


def load_folder(folder_id: str) -> dict:
    metadata = load_metadata(folder_id)
    if not metadata or metadata.get("type") != "folder":
        raise HTTPException(status_code=404, detail="Folder not found")
    if is_expired(metadata):
        logger.info("Requested expired folder %s", folder_id)
        cleanup_file(folder_id)
        raise HTTPException(status_code=410, detail=EXPIRED_MESSAGE)
    return metadata

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


def render_folder_page(folder_id: str, metadata: dict, request: Request) -> str:
    quoted_folder_id = quote(folder_id, safe="")
    has_password = metadata.get("has_password", False)
    authorized = not has_password
    if has_password:
        authorized = verify_folder_token(request.cookies.get(FOLDER_COOKIE), folder_id)

    files = metadata.get("files", []) if authorized else []
    files_json = json.dumps(files, ensure_ascii=False)
    files_json = files_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

    return FOLDER_TEMPLATE_PATH.read_text(encoding="utf-8").replace(
        "__FOLDER_ID_URL__",
        quoted_folder_id,
    ).replace(
        "__FOLDER_ID__",
        escape(folder_id),
    ).replace(
        "__FOLDER_HAS_PASSWORD__",
        "true" if has_password else "false",
    ).replace(
        "__FOLDER_AUTHORIZED__",
        "true" if authorized else "false",
    ).replace(
        "__FOLDER_FILES__",
        files_json,
    ).replace(
        "__FOLDER_EXPIRY__",
        escape(format_expiry_label(metadata.get("expires_at"))),
    ).replace(
        "__FOLDER_VIEWS__",
        escape(str(metadata.get("views", 0))),
    ).replace(
        "__FILE_COUNT__",
        str(len(metadata.get("files", []))),
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
async def upload_file(
    file: UploadFile = File(...),
    Authorisation: Annotated[str | None, Form()] = None,
    password: Annotated[str | None, Form()] = None,
    slug: Annotated[str | None, Form()] = None,
    is_static: Annotated[bool | None, Form()] = False,
    duration: Annotated[int | None, Form()] = EXPIRY_HOURS,
    duration_unit: Annotated[str | None, Form()] = "hours",
):

    duration_hours = validate_duration(duration, duration_unit)
    expiry_duration = timedelta(hours=duration_hours)

    if file.size is not None and file.size > MAX_SIZE:
        logger.warning("Rejected upload for %s: file too large", file.filename)
        raise HTTPException(status_code=413, detail="File too large")
    
    if slug:
        validate_slug_input(slug)

    cleanup_expired_files()

    file_id = generate_file_id(slug)
    file_path = UPLOAD_DIR / file_id

    if object_exists(file_id):
        raise HTTPException(status_code=409, detail="Slug already in use")

    temp_path = None
    try:
        if storage.remote:
            temp_file = tempfile.NamedTemporaryFile(delete=False)
            temp_path = Path(temp_file.name)
            with temp_file:
                shutil.copyfileobj(file.file, temp_file)
            size_bytes = temp_path.stat().st_size
            if size_bytes > MAX_SIZE:
                raise HTTPException(status_code=413, detail="File too large")
            with temp_path.open("rb") as stored_file:
                storage.put_file(stored_file, object_key(file_id), mimetypes.guess_type(file.filename or "")[0])
        else:
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            size_bytes = file_path.stat().st_size
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)

    password_hash = None
    auth_value = Authorisation or password
    if auth_value:
        password_hash = hash_password(auth_value)

    write_metadata(
        file_id,
        file.filename or file_id,
        is_static=is_static,
        size_bytes=size_bytes,
        password_hash=password_hash,
        expiry_duration=expiry_duration,
    )    
    logger.info("Stored upload %s as %s", file.filename, file_id)

    response = {
        "id": file_id,
        "original_name": file.filename,
        "expires_in_hours": duration_hours,
        "duration": duration,
        "duration_unit": duration_unit,
        "is_static": bool(is_static),
    }
    if is_static:
        response["static_url"] = f"/static/{file_id}"

    return response


@app.post("/folder/")
async def upload_folder(
    files: Annotated[list[UploadFile], File(...)],
    Authorisation: Annotated[str | None, Form()] = None,
    password: Annotated[str | None, Form()] = None,
    slug: Annotated[str | None, Form()] = None,
    duration: Annotated[int | None, Form()] = EXPIRY_HOURS,
    duration_unit: Annotated[str | None, Form()] = "hours",
):

    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    duration_hours = validate_duration(duration, duration_unit)

    if slug:
        validate_slug_input(slug)

    cleanup_expired_files()

    folder_id = generate_folder_id(slug)

    if slug and id_in_use(slug):
        raise HTTPException(status_code=409, detail="Slug already in use")

    folder_dir = UPLOAD_DIR / folder_id
    folder_dir.mkdir(exist_ok=False)

    file_entries = []
    try:
        for uploaded in files:
            file_id = generate_file_id()
            for _ in range(10):
                if not (folder_dir / file_id).exists():
                    break
                file_id = generate_file_id()

            dest = folder_dir / file_id
            size = 0
            with open(dest, "wb") as buffer:
                while True:
                    chunk = uploaded.file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_SIZE:
                        raise HTTPException(
                            status_code=413,
                            detail=f"File too large: {uploaded.filename or file_id}",
                        )
                    buffer.write(chunk)

            file_entries.append({
                "id": file_id,
                "name": uploaded.filename or file_id,
                "size_bytes": size,
            })
    except HTTPException:
        shutil.rmtree(folder_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(folder_dir, ignore_errors=True)
        logger.exception("Folder upload failed for %s", folder_id)
        raise HTTPException(status_code=500, detail="Folder upload failed")

    if storage.remote:
        try:
            for entry in file_entries:
                local_path = folder_dir / entry["id"]
                with local_path.open("rb") as stored_file:
                    storage.put_file(
                        stored_file,
                        object_key(folder_id, entry["id"]),
                        mimetypes.guess_type(entry["name"])[0],
                    )
        except Exception:
            storage.delete_prefix(object_key(folder_id) + "/")
            shutil.rmtree(folder_dir, ignore_errors=True)
            logger.exception("Folder upload failed for %s", folder_id)
            raise HTTPException(status_code=500, detail="Folder upload failed")
        shutil.rmtree(folder_dir, ignore_errors=True)

    password_hash = None
    auth_value = Authorisation or password
    if auth_value:
        password_hash = hash_password(auth_value)

    metadata = {
        "type": "folder",
        "password_hash": password_hash,
        "has_password": password_hash is not None,
        "expires_at": (utc_now() + timedelta(hours=duration_hours)).isoformat(),
        "views": 0,
        "is_static": False,
        "files": file_entries,
    }
    metadata_path(folder_id).write_text(json.dumps(metadata), encoding="utf-8")
    logger.info("Stored folder %s with %s file(s)", folder_id, len(file_entries))

    return {
        "id": folder_id,
        "type": "folder",
        "file_count": len(file_entries),
        "files": [{"id": entry["id"], "name": entry["name"]} for entry in file_entries],
        "expires_in_hours": duration_hours,
        "duration": duration,
        "duration_unit": duration_unit,
    }


@app.get("/download.css")
async def download_styles():
    return FileResponse(DOWNLOAD_STYLES_PATH)


@app.get("/folder.css")
async def folder_styles():
    return FileResponse(FOLDER_STYLES_PATH)


@app.get("/paste.css")
async def paste_styles():
    return FileResponse(PASTE_STYLES_PATH)


@app.get("/paste/{paste_id}", response_class=HTMLResponse)
async def get_paste(paste_id: str):
    if not SLUG_PATTERN.fullmatch(paste_id):
        raise HTTPException(status_code=404, detail="Paste not found")

    paste_file_path = UPLOAD_DIR / "pastes" / f"{paste_id}.txt"
    if not paste_file_path.is_file():
        raise HTTPException(status_code=404, detail="Paste not found")

    try:
        template = PASTE_TEMPLATE_PATH.read_text(encoding="utf-8")
        return HTMLResponse(template.replace("__PASTE_ID__", paste_id))
    except OSError:
        logger.exception("Failed to render paste %s", paste_id)
        raise HTTPException(status_code=500, detail="Failed to load paste")


@app.get("/paste/{paste_id}/raw", response_class=PlainTextResponse)
async def get_paste_raw(
    paste_id: str,
    password: Annotated[str | None, Query()] = None,
    header_password: Annotated[str | None, Header(alias="X-Paste-Password")] = None,
):
    if not SLUG_PATTERN.fullmatch(paste_id):
        raise HTTPException(status_code=404, detail="Paste not found")

    paste_file_path = UPLOAD_DIR / "pastes" / f"{paste_id}.txt"
    if not paste_file_path.is_file():
        raise HTTPException(status_code=404, detail="Paste not found")

    try:
        metadata = json.loads(paste_metadata_path(paste_id).read_text(encoding="utf-8"))
        password_hash = metadata.get("password_hash")
        supplied_password = header_password or password
        if password_hash:
            if not supplied_password:
                raise HTTPException(status_code=401, detail="Paste password required")
            try:
                ph.verify(password_hash, supplied_password)
            except VerifyMismatchError:
                raise HTTPException(status_code=401, detail="Invalid paste password")

        return PlainTextResponse(paste_file_path.read_text(encoding="utf-8"))
    except HTTPException:
        raise
    except OSError:
        logger.exception("Failed to read paste %s", paste_id)
        raise HTTPException(status_code=500, detail="Failed to read paste")


@app.get("/{file_id}", response_class=HTMLResponse)
async def read_items(file_id: str, request: Request):
    metadata = load_metadata(file_id)
    if metadata and is_expired(metadata):
        logger.info("Landing page requested for expired file %s", file_id)
        cleanup_file(file_id)
        return JSONResponse(
            status_code=410,
        )

    if not metadata:
        logger.warning("Landing page requested for missing file %s", file_id)
        raise HTTPException(status_code=404, detail="File not found")

    if metadata.get("type") == "folder":
        increment_views(file_id, metadata)
        logger.info("Rendering folder page for %s", file_id)
        return HTMLResponse(content=render_folder_page(file_id, metadata, request))

    if not object_exists(file_id):
        logger.warning("Landing page requested for missing file %s", file_id)
        raise HTTPException(status_code=404, detail="File not found")

    increment_views(file_id, metadata)
    logger.info("Rendering download page for %s", file_id)
    file_path, temporary = materialize_object(file_id)
    try:
        content = render_download_page(file_id, metadata, file_path)
    finally:
        if temporary:
            file_path.unlink(missing_ok=True)
    return HTMLResponse(content=content)

@app.get("/static/{file_id}")
async def server_satic_file(file_id: str):
    metadata = load_metadata(file_id)

    if not metadata or not metadata.get("is_static"):
        logger.warning(
            "Static file requested for missing or non-static file %s",
            file_id
        )
        raise HTTPException(status_code=404, detail="File not found")

    if not object_exists(file_id):
        logger.warning("Static file requested for missing file %s", file_id)
        raise HTTPException(status_code=404, detail="File not found")

    original_name = metadata.get("original_name", file_id)
    media_type, _ = mimetypes.guess_type(original_name)

    logger.info(
        "Serving static file %s as %s",
        file_id,
        media_type or "application/octet-stream"
    )

    file_path, temporary = materialize_object(file_id)
    background = BackgroundTask(file_path.unlink) if temporary else None
    return FileResponse(
        path=file_path,
        media_type=media_type or "application/octet-stream",
        background=background,
    )

@app.post("/folder/{folder_id}/auth")
async def authorize_folder(
    folder_id: str,
    password: Annotated[str | None, Form()] = None,
):
    metadata = load_folder(folder_id)

    if not metadata.get("has_password"):
        return {"ok": True}

    if not password or not metadata.get("password_hash"):
        logger.warning("Unauthorized folder auth attempt for %s", folder_id)
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        ph.verify(metadata["password_hash"], password)
    except VerifyMismatchError:
        logger.warning("Wrong password for folder %s", folder_id)
        raise HTTPException(status_code=401, detail="Unauthorized")

    token = create_folder_token(folder_id)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        key=FOLDER_COOKIE,
        value=token,
        max_age=FOLDER_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    logger.info("Folder %s authorized", folder_id)
    return response


@app.get("/folder/{folder_id}/files/{file_id}")
async def download_folder_file(
    folder_id: str,
    file_id: str,
    request: Request,
    Authorisation: Annotated[str | None, Header()] = None,
    password: Annotated[str | None, Header()] = None,
):
    metadata = load_folder(folder_id)

    if not is_folder_authorized(folder_id, metadata, request, Authorisation or password):
        logger.warning("Unauthorized folder file download attempt %s/%s", folder_id, file_id)
        raise HTTPException(status_code=401, detail="Unauthorized")

    entry = next((entry for entry in metadata.get("files", []) if entry["id"] == file_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="File not found")

    if not object_exists(folder_id, file_id):
        raise HTTPException(status_code=404, detail="File not found")

    logger.info("Serving folder file %s from %s as %s", file_id, folder_id, entry["name"])
    path, temporary = materialize_object(folder_id, file_id)
    background = BackgroundTask(path.unlink) if temporary else None
    return FileResponse(path=path, filename=entry["name"], background=background)


@app.get("/folder/{folder_id}/download")
async def download_folder_files(
    folder_id: str,
    request: Request,
    files: Annotated[str | None, Query()] = None,
    Authorisation: Annotated[str | None, Header()] = None,
    password: Annotated[str | None, Header()] = None,
):
    metadata = load_folder(folder_id)

    if not is_folder_authorized(folder_id, metadata, request, Authorisation or password):
        logger.warning("Unauthorized folder download attempt %s", folder_id)
        raise HTTPException(status_code=401, detail="Unauthorized")

    entries = metadata.get("files", [])
    if files:
        selected_ids = {item for item in files.split(",") if item}
        entries = [entry for entry in entries if entry["id"] in selected_ids]
        if not entries:
            raise HTTPException(status_code=400, detail="No matching files")

    if not entries:
        raise HTTPException(status_code=404, detail="Folder is empty")

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    temp_path = temp_file.name
    temp_file.close()

    try:
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry in entries:
                if not object_exists(folder_id, entry["id"]):
                    continue
                path, temporary = materialize_object(folder_id, entry["id"])
                try:
                    archive.write(path, arcname=entry["name"])
                finally:
                    if temporary:
                        path.unlink(missing_ok=True)
    except Exception:
        os.unlink(temp_path)
        raise

    zip_name = f"{folder_id}_files.zip"
    logger.info("Serving %s file(s) from folder %s as %s", len(entries), folder_id, zip_name)
    return FileResponse(
        path=temp_path,
        media_type="application/zip",
        filename=zip_name,
        background=BackgroundTask(os.unlink, temp_path),
    )
    
@app.get("/files/{file_id}")
async def get_file(
    file_id: str,
    Authorisation: Annotated[str | None, Header()] = None,
    password: Annotated[str | None, Header()] = None,
):
    metadata = load_metadata(file_id)
    if metadata and is_expired(metadata):
        logger.info("Download requested for expired file %s", file_id)
        cleanup_file(file_id)
        return JSONResponse(
            status_code=410,
            content={"error": EXPIRED_MESSAGE},
        )

    if metadata and metadata.get("type") == "folder":
        logger.warning("Download requested for folder via file route %s", file_id)
        raise HTTPException(status_code=404, detail="File not found")

    if not object_exists(file_id):
        logger.warning("Download requested for missing file %s", file_id)
        raise HTTPException(status_code=404, detail="File not found")

    if metadata and metadata.get("has_password"):
        password_hash = metadata.get("password_hash")

        auth_value = Authorisation or password

        if not auth_value or not password_hash:
            logger.warning("Unauthorized download attempt for protected file %s", file_id)
            raise HTTPException(status_code=401, detail="Unauthorized")

        try:
            ph.verify(password_hash, auth_value)
        except VerifyMismatchError:
            logger.warning("Unauthorized download attempt for protected file %s", file_id)
            raise HTTPException(status_code=401, detail="Unauthorized")

    filename = metadata["original_name"] if metadata else file_id
    logger.info("Serving file %s as %s", file_id, filename)
    file_path, temporary = materialize_object(file_id)
    background = BackgroundTask(file_path.unlink) if temporary else None
    return FileResponse(path=file_path, filename=filename, background=background)

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
    if metadata.get("type") == "folder":
        return {
            "type": "folder",
            "file_count": len(metadata.get("files", [])),
            "files": [
                {"id": entry["id"], "name": entry["name"], "size_bytes": entry.get("size_bytes")}
                for entry in metadata.get("files", [])
            ],
            "expires_at": metadata["expires_at"],
            "views": metadata.get("views", 0),
            "has_password": metadata.get("has_password", False),
        }

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
        cleanup_file(file_id)
        logging.info("Deleted file %s via API", file_id)
        return {"detail": "File deleted"}

@app.post("/paste/add")
async def add_paste(request: Request) -> dict[str, str | bool]:
    try:
        request_data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")

    if not isinstance(request_data, dict) or not isinstance(request_data.get("data"), str):
        raise HTTPException(status_code=400, detail="Request body must contain a string 'data' field")

    slug = request_data.get("slug")
    password = request_data.get("password")
    if slug is not None and not isinstance(slug, str):
        raise HTTPException(status_code=400, detail="Paste slug must be a string")
    if password is not None and not isinstance(password, str):
        raise HTTPException(status_code=400, detail="Paste password must be a string")
    if slug:
        validate_slug_input(slug)
        if id_in_use(slug):
            raise HTTPException(status_code=409, detail="Paste slug already exists")

    paste_id = generate_file_id(slug)
    password_hash = hash_password(password) if password else None

    try:
        created_paste_id = create_paste(paste_id, request_data["data"], password_hash)
    except FileExistsError:
        logger.warning("Paste id collision for %s", paste_id)
        raise HTTPException(status_code=409, detail="Paste id already exists")
    except OSError:
        logger.exception("Failed to create paste %s", paste_id)
        raise HTTPException(status_code=500, detail="Failed to create paste")

    return {
        "id": created_paste_id,
        "has_password": bool(password_hash),
    }

@app.exception_handler(404)
async def custom_404_handler(request: Request, exc):
    not_found_path = SRC_DIR / "public" / "404.html"
    return FileResponse(path=str(not_found_path), status_code=404)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=service_port, reload=False)

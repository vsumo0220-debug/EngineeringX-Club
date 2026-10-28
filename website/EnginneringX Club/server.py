from __future__ import annotations

import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image, UnidentifiedImageError


SITE_DIR = Path(__file__).resolve().parent
ROOT_DIR = SITE_DIR.parent.parent
DATA_DIR = Path(os.environ.get("ENGINEERINGX_DATA_DIR", ROOT_DIR / "data")).resolve()
DB_PATH = DATA_DIR / "projects.sqlite3"
UPLOAD_DIR = DATA_DIR / "uploads"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_IMAGE_BYTES + 32 * 1024
SESSION_SECONDS = 12 * 60 * 60
CATEGORIES = {
    "Robotics & Mechatronics",
    "Electronics & Embedded Systems",
    "Mechanical Engineering",
    "Software & Computing",
    "CAD & Prototyping",
    "Electrical Engineering",
    "Architecture & Design",
}
FIELD_LIMITS = {
    "title": 120,
    "creator_display_name": 80,
    "description": 5000,
    "category": 80,
    "github_url": 2048,
    "youtube_url": 2048,
}


class RequestError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def validate_project_url(value: str, kind: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if len(value) > FIELD_LIMITS[f"{kind}_url"] or any(ord(char) < 32 for char in value):
        raise RequestError(HTTPStatus.BAD_REQUEST, f"Invalid {kind} URL.")
    try:
        parsed = urlparse(value)
    except ValueError as error:
        raise RequestError(HTTPStatus.BAD_REQUEST, f"Invalid {kind} URL.") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise RequestError(HTTPStatus.BAD_REQUEST, f"Invalid {kind} URL.")
    hostname = parsed.hostname.lower().rstrip(".")
    if kind == "github":
        allowed = hostname in {"github.com", "www.github.com"}
    else:
        allowed = hostname in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
    if not allowed or not parsed.path.strip("/"):
        raise RequestError(HTTPStatus.BAD_REQUEST, f"Use a valid {kind.title()} project link.")
    return value


def validate_image(content: bytes) -> tuple[str, str]:
    if len(content) > MAX_IMAGE_BYTES:
        raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Choose an image smaller than 5 MB.")
    formats = {
        "JPEG": ("image/jpeg", ".jpg"),
        "PNG": ("image/png", ".png"),
        "WEBP": ("image/webp", ".webp"),
    }
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                image_format = image.format
                if image_format not in formats or image.width * image.height > 16_000_000:
                    raise RequestError(
                        HTTPStatus.BAD_REQUEST,
                        "Upload a JPEG, PNG, or WebP image smaller than 16 megapixels.",
                    )
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise RequestError(HTTPStatus.BAD_REQUEST, "Upload a valid JPEG, PNG, or WebP project image.") from error
    if image_format in formats:
        return formats[image_format]
    raise RequestError(HTTPStatus.BAD_REQUEST, "Upload a valid JPEG, PNG, or WebP project image.")


class EngineeringXApp:
    def __init__(
        self,
        data_dir: Path = DATA_DIR,
        site_dir: Path = SITE_DIR,
        admin_password: str | None = None,
    ):
        self.data_dir = data_dir.resolve()
        self.site_dir = site_dir.resolve()
        self.upload_dir = self.data_dir / "uploads"
        self.db_path = self.data_dir / "projects.sqlite3"
        self.admin_password = admin_password or os.environ.get("ENGINEERINGX_ADMIN_PASSWORD", "")
        self.sessions: dict[str, float] = {}
        self.login_failures: dict[str, tuple[int, float]] = {}
        self.lock = threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 120),
                    creator_display_name TEXT NOT NULL CHECK(length(creator_display_name) BETWEEN 1 AND 80),
                    description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 5000),
                    category TEXT NOT NULL CHECK(length(category) BETWEEN 1 AND 80),
                    cover_image_path TEXT NOT NULL,
                    github_url TEXT,
                    youtube_url TEXT,
                    moderation_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(moderation_status IN ('pending', 'approved', 'rejected')),
                    created_at TEXT NOT NULL
                )
                """
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def public_projects(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id, title, creator_display_name, description, category,
                          cover_image_path, github_url, youtube_url, created_at
                   FROM projects WHERE moderation_status = 'approved'
                   ORDER BY created_at DESC, id DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def admin_projects(self, status: str) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id, title, creator_display_name, description, category,
                          cover_image_path, github_url, youtube_url, moderation_status, created_at
                   FROM projects WHERE moderation_status = ?
                   ORDER BY created_at DESC, id DESC""",
                (status,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_submission(self, fields: dict[str, str], image: bytes) -> int:
        required = ("title", "creator_display_name", "description", "category")
        for field in required:
            if not fields.get(field, "").strip():
                label = field.replace("_", " ").capitalize()
                raise RequestError(HTTPStatus.BAD_REQUEST, f"{label} is required.")
            if len(fields[field].strip()) > FIELD_LIMITS[field]:
                raise RequestError(HTTPStatus.BAD_REQUEST, f"{field.replace('_', ' ').capitalize()} is too long.")
        values = {field: fields[field].strip() for field in required}
        if values["category"] not in CATEGORIES:
            raise RequestError(HTTPStatus.BAD_REQUEST, "Choose a valid engineering category.")
        values["github_url"] = validate_project_url(fields.get("github_url", ""), "github")
        values["youtube_url"] = validate_project_url(fields.get("youtube_url", ""), "youtube")
        _, extension = validate_image(image)
        filename = f"{uuid.uuid4().hex}{extension}"
        image_path = self.upload_dir / filename
        image_path.write_bytes(image)
        try:
            with self.connect() as connection:
                cursor = connection.execute(
                    """INSERT INTO projects
                       (title, creator_display_name, description, category, cover_image_path,
                        github_url, youtube_url, moderation_status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (
                        values["title"],
                        values["creator_display_name"],
                        values["description"],
                        values["category"],
                        filename,
                        values["github_url"],
                        values["youtube_url"],
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                return int(cursor.lastrowid)
        except Exception:
            image_path.unlink(missing_ok=True)
            raise


def make_handler(app: EngineeringXApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "EngineeringX/1.0"

        def log_message(self, format_string: str, *args) -> None:
            if os.environ.get("ENGINEERINGX_ACCESS_LOG") == "1":
                super().log_message(format_string, *args)

        def send_json(self, status: HTTPStatus, payload: dict | list, headers: dict | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            if headers:
                for key, value in headers.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def read_body(self, limit: int = MAX_REQUEST_BYTES) -> bytes:
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise RequestError(HTTPStatus.BAD_REQUEST, "Invalid request length.") from error
            if size <= 0 or size > limit:
                raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request is empty or exceeds the upload limit.")
            return self.rfile.read(size)

        def form_data(self) -> tuple[dict[str, str], bytes]:
            content_type = self.headers.get("Content-Type", "")
            if "\r" in content_type or "\n" in content_type or not content_type.lower().startswith("multipart/form-data;"):
                raise RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Submit the project as a multipart form.")
            body = self.read_body()
            try:
                content_type_header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii")
            except UnicodeEncodeError as error:
                raise RequestError(HTTPStatus.BAD_REQUEST, "Invalid multipart content type.") from error
            message = BytesParser(policy=policy.default).parsebytes(
                content_type_header + body
            )
            if not message.is_multipart():
                raise RequestError(HTTPStatus.BAD_REQUEST, "Invalid multipart project submission.")
            fields: dict[str, str] = {}
            image: bytes | None = None
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if not name:
                    continue
                content = part.get_payload(decode=True) or b""
                if name == "image":
                    if image is not None or not part.get_filename():
                        raise RequestError(HTTPStatus.BAD_REQUEST, "Upload exactly one project image.")
                    image = content
                elif name in FIELD_LIMITS:
                    if name in fields:
                        raise RequestError(HTTPStatus.BAD_REQUEST, "Duplicate project fields are not allowed.")
                    try:
                        fields[name] = content.decode("utf-8")
                    except UnicodeDecodeError as error:
                        raise RequestError(HTTPStatus.BAD_REQUEST, "Project fields must be UTF-8 text.") from error
                else:
                    raise RequestError(HTTPStatus.BAD_REQUEST, "Unexpected project form field.")
            if image is None:
                raise RequestError(HTTPStatus.BAD_REQUEST, "Upload a project image before submitting.")
            return fields, image

        def session_token(self) -> str | None:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except CookieError:
                return None
            morsel = cookie.get("engineeringx_admin")
            return morsel.value if morsel else None

        def require_admin(self) -> bool:
            token = self.session_token()
            if token is None:
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Administrator sign-in is required."})
                return False
            with app.lock:
                expires = app.sessions.get(token)
                if expires is None or expires <= time.time():
                    app.sessions.pop(token, None)
                    self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Administrator session expired. Sign in again."})
                    return False
            return True

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/projects":
                self.send_json(HTTPStatus.OK, app.public_projects())
                return
            admin_image = re.fullmatch(r"/api/admin/projects/(\d+)/image", parsed.path)
            if admin_image:
                if not self.require_admin():
                    return
                with app.connect() as connection:
                    row = connection.execute(
                        "SELECT cover_image_path FROM projects WHERE id = ?",
                        (int(admin_image.group(1)),),
                    ).fetchone()
                if row is None:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Project not found."})
                    return
                file_path = app.upload_dir / row["cover_image_path"]
                try:
                    image = file_path.read_bytes()
                except FileNotFoundError:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Project image not found."})
                    return
                except OSError:
                    self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "The project image could not be read."})
                    return
                media_type = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[file_path.suffix]
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", media_type)
                self.send_header("Content-Length", str(len(image)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(image)
                return
            if parsed.path == "/api/admin/projects":
                if not self.require_admin():
                    return
                status = parse_qs(parsed.query).get("status", ["pending"])[0]
                if status not in {"pending", "approved", "rejected"}:
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid moderation status."})
                    return
                self.send_json(HTTPStatus.OK, app.admin_projects(status))
                return
            if parsed.path.startswith("/uploads/"):
                filename = unquote(parsed.path.removeprefix("/uploads/"))
                if not re.fullmatch(r"[a-f0-9]{32}\.(?:jpg|png|webp)", filename):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                with app.connect() as connection:
                    row = connection.execute(
                        "SELECT 1 FROM projects WHERE cover_image_path = ? AND moderation_status = 'approved'",
                        (filename,),
                    ).fetchone()
                if not row:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                file_path = app.upload_dir / filename
                try:
                    image = file_path.read_bytes()
                except FileNotFoundError:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                except OSError:
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                media_type = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[file_path.suffix]
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", media_type)
                self.send_header("Content-Length", str(len(image)))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(image)
                return
            if parsed.path == "/":
                self.path = "/index.html"
                parsed = urlparse(self.path)
            if parsed.path == "/admin":
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                self.send_header("Location", "/#community-projects")
                self.end_headers()
                return
            relative_path = unquote(parsed.path.lstrip("/"))
            allowed_assets = {"index.html", "assets/engineeringx-club-logo.jpg"}
            if relative_path not in allowed_assets:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            candidate = app.site_dir / relative_path
            if not candidate.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = candidate.read_bytes()
            media_type = {
                ".html": "text/html; charset=utf-8",
                ".jpg": "image/jpeg",
            }.get(candidate.suffix, "application/octet-stream")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            try:
                if self.path == "/api/projects":
                    fields, image = self.form_data()
                    project_id = app.save_submission(fields, image)
                    self.send_json(
                        HTTPStatus.CREATED,
                        {"id": project_id, "moderation_status": "pending", "message": "Project submitted for moderation."},
                    )
                    return
                if self.path == "/api/admin/login":
                    self.admin_login()
                    return
                if self.path == "/api/admin/logout":
                    if not self.require_admin():
                        return
                    if self.headers.get("X-Admin-Action") != "1":
                        raise RequestError(HTTPStatus.FORBIDDEN, "Administrator action header is required.")
                    token = self.session_token()
                    if token:
                        with app.lock:
                            app.sessions.pop(token, None)
                    self.send_json(
                        HTTPStatus.OK,
                        {"message": "Signed out."},
                        {"Set-Cookie": "engineeringx_admin=; HttpOnly; SameSite=Strict; Path=/api/admin; Max-Age=0"},
                    )
                    return
                if self.path.startswith("/api/admin/projects/"):
                    if not self.require_admin():
                        return
                    if self.headers.get("X-Admin-Action") != "1":
                        raise RequestError(HTTPStatus.FORBIDDEN, "Administrator action header is required.")
                    self.moderate_project()
                    return
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            except RequestError as error:
                self.send_json(error.status, {"error": str(error)})
            except sqlite3.Error:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "The project could not be saved. Please try again."})
            except OSError:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "The project could not be saved. Check server storage permissions."})

        def admin_login(self) -> None:
            if not app.admin_password:
                raise RequestError(HTTPStatus.SERVICE_UNAVAILABLE, "Administrator sign-in is not configured.")
            ip = self.client_address[0]
            failures, blocked_until = app.login_failures.get(ip, (0, 0))
            if blocked_until > time.time():
                raise RequestError(HTTPStatus.TOO_MANY_REQUESTS, "Too many sign-in attempts. Try again later.")
            try:
                data = json.loads(self.read_body(4096))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise RequestError(HTTPStatus.BAD_REQUEST, "Invalid sign-in request.") from error
            password = data.get("password", "") if isinstance(data, dict) else ""
            if not isinstance(password, str) or not secrets.compare_digest(password, app.admin_password):
                failures += 1
                if failures >= 5:
                    app.login_failures[ip] = (failures, time.time() + 15 * 60)
                else:
                    app.login_failures[ip] = (failures, 0)
                raise RequestError(HTTPStatus.UNAUTHORIZED, "Administrator password is incorrect.")
            app.login_failures.pop(ip, None)
            token = secrets.token_urlsafe(32)
            with app.lock:
                app.sessions[token] = time.time() + SESSION_SECONDS
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            self.send_json(
                HTTPStatus.OK,
                {"message": "Signed in."},
                {"Set-Cookie": f"engineeringx_admin={token}; HttpOnly; SameSite=Strict; Path=/api/admin; Max-Age={SESSION_SECONDS}{secure}"},
            )

        def moderate_project(self) -> None:
            match = re.fullmatch(r"/api/admin/projects/(\d+)(?:/(moderation))?", self.path)
            if not match:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Project not found."})
                return
            project_id = int(match.group(1))
            with app.connect() as connection:
                row = connection.execute(
                    "SELECT cover_image_path FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
                if row is None:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Project not found."})
                    return
                if match.group(2) is None:
                    connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
                    image_path = app.upload_dir / row["cover_image_path"]
                    image_path.unlink(missing_ok=True)
                    self.send_json(HTTPStatus.OK, {"message": "Project deleted."})
                    return
                try:
                    data = json.loads(self.read_body(4096))
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise RequestError(HTTPStatus.BAD_REQUEST, "Invalid moderation request.") from error
                status = data.get("moderation_status") if isinstance(data, dict) else None
                if status not in {"approved", "rejected"}:
                    raise RequestError(HTTPStatus.BAD_REQUEST, "Choose approved or rejected.")
                connection.execute(
                    "UPDATE projects SET moderation_status = ? WHERE id = ?", (status, project_id)
                )
            self.send_json(HTTPStatus.OK, {"moderation_status": status})

        def do_DELETE(self) -> None:
            try:
                if not self.path.startswith("/api/admin/projects/"):
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                    return
                if not self.require_admin():
                    return
                if self.headers.get("X-Admin-Action") != "1":
                    raise RequestError(HTTPStatus.FORBIDDEN, "Administrator action header is required.")
                self.moderate_project()
            except RequestError as error:
                self.send_json(error.status, {"error": str(error)})
            except sqlite3.Error:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "The project could not be deleted."})
            except OSError:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "The project could not be deleted. Check server storage permissions."})

    return Handler


def main() -> None:
    password = os.environ.get("ENGINEERINGX_ADMIN_PASSWORD", "")
    if len(password) < 16:
        raise SystemExit("Set ENGINEERINGX_ADMIN_PASSWORD to a unique password with at least 16 characters.")
    app = EngineeringXApp(admin_password=password)
    host = os.environ.get("ENGINEERINGX_HOST", "127.0.0.1")
    port = int(os.environ.get("ENGINEERINGX_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"EngineeringX is available at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping EngineeringX server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

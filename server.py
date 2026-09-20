#!/usr/bin/env python3
"""Small, filesystem-backed MCP server for a Hugo site.

The site content is the source of truth. Mutations are atomic, guarded by a
revision hash, recorded in an audit log, and require a request_id for
idempotency. Deletes are soft-deletes into a private trash directory.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any


SITE_ROOT = Path(os.environ.get("HUGO_SITE_ROOT", "/site")).resolve()


def site_child(env_name: str, default: str) -> Path:
    """Resolve a configurable site directory without allowing path escape."""
    configured = os.environ.get(env_name, default)
    candidate = (SITE_ROOT / configured).resolve()
    if candidate != SITE_ROOT and SITE_ROOT not in candidate.parents:
        raise RuntimeError(f"{env_name} must stay below HUGO_SITE_ROOT")
    return candidate


POSTS_ROOT = site_child("HUGO_POSTS_DIR", "content/posts")
PUBLIC_ROOT = site_child("HUGO_PUBLIC_DIR", "public")
DATA_ROOT = Path(os.environ.get("HUGO_MCP_DATA_ROOT", "/data")).resolve()
TRASH_ROOT = DATA_ROOT / "trash"
BACKUP_ROOT = DATA_ROOT / "backups"
AUDIT_LOG = DATA_ROOT / "audit.jsonl"
IDEMPOTENCY_ROOT = DATA_ROOT / "idempotency"
TOKEN_FILE = Path(os.environ.get("HUGO_MCP_TOKEN_FILE", str(DATA_ROOT / "token"))).resolve()
HUGO_BIN = os.environ.get("HUGO_BIN", "/usr/local/bin/hugo")
MAX_BODY = int(os.environ.get("HUGO_MCP_MAX_BODY", str(8 * 1024 * 1024)))
MEDIA_MAX_BYTES = int(os.environ.get("HUGO_MCP_MEDIA_MAX_BYTES", str(5 * 1024 * 1024)))
BUILD_TIMEOUT = int(os.environ.get("HUGO_BUILD_TIMEOUT", "120"))

SITE_ROOT_STR = str(SITE_ROOT)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,120}$")
REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,120}$")
MEDIA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}$")
MEDIA_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MEDIA_EXTENSIONS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
MUTATION_LOCK = threading.RLock()
RATE_LOCK = threading.Lock()
RATE_STATE: dict[str, list[float]] = {}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message, "retryable": False}
    if details is not None:
        result["details"] = details
    return result


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "false"}:
        return value == "true"
    if value in {"null", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return [part.strip().strip("\"'") for part in value[1:-1].split(",") if part.strip()]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_markdown(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    raw = text[4:end]
    body = text[end + 4 :]
    if body.startswith("\n"):
        body = body[1:]
    front: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        front[key.strip()] = parse_scalar(value)
    return front, body


def quote_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def render_markdown(front: dict[str, Any], body: str) -> str:
    order = ["title", "date", "lastmod", "slug", "draft", "categories", "tags", "summary"]
    keys = order + [key for key in front if key not in order]
    lines = ["---"]
    for key in keys:
        if key not in front or front[key] is None:
            continue
        value = front[key]
        if isinstance(value, list):
            value_text = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            value_text = "true" if value else "false"
        elif isinstance(value, (int, float)):
            value_text = str(value)
        else:
            value_text = quote_string(str(value))
        lines.append(f"{key}: {value_text}")
    lines.extend(["---", "", body.rstrip(), ""])
    return "\n".join(lines)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_roots() -> None:
    POSTS_ROOT.mkdir(parents=True, exist_ok=True)
    for path in (DATA_ROOT, TRASH_ROOT, BACKUP_ROOT, IDEMPOTENCY_ROOT):
        path.mkdir(parents=True, exist_ok=True)


def safe_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    return normalized[:120] or f"post-{int(time.time())}"


def post_path(slug: str) -> Path:
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ValueError("slug must contain only lowercase letters, numbers, and hyphens")
    path = (POSTS_ROOT / f"{slug}.md").resolve()
    if path.parent != POSTS_ROOT:
        raise ValueError("invalid slug path")
    return path


def bundle_root(slug: str) -> Path:
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ValueError("slug must contain only lowercase letters, numbers, and hyphens")
    root = (POSTS_ROOT / slug).resolve()
    if root.parent != POSTS_ROOT:
        raise ValueError("invalid bundle path")
    return root


def bundle_post_path(slug: str) -> Path:
    return bundle_root(slug) / "index.md"


def is_bundle(path: Path) -> bool:
    return path.name == "index.md" and path.parent.parent == POSTS_ROOT


def iter_post_paths() -> list[Path]:
    paths = list(POSTS_ROOT.glob("*.md"))
    paths.extend(root / "index.md" for root in POSTS_ROOT.iterdir() if root.is_dir() and (root / "index.md").is_file())
    return sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True)


def post_summary(path: Path) -> dict[str, Any]:
    front, body = parse_markdown(path)
    default_slug = path.parent.name if is_bundle(path) else path.stem
    slug = str(front.get("slug") or default_slug)
    return {
        "slug": slug,
        "path": path.relative_to(SITE_ROOT).as_posix(),
        "bundle": is_bundle(path),
        "title": str(front.get("title") or slug),
        "date": front.get("date"),
        "lastmod": front.get("lastmod"),
        "draft": bool(front.get("draft", False)),
        "status": "draft" if bool(front.get("draft", False)) else "publish",
        "categories": front.get("categories", []),
        "tags": front.get("tags", []),
        "summary": front.get("summary"),
        "revision": sha256_file(path),
        "word_count": len(re.findall(r"\S+", body)),
    }


def find_post(slug: str) -> Path:
    direct = post_path(slug)
    bundled = bundle_post_path(slug)
    matches = [path for path in (direct, bundled) if path.exists()]
    if len(matches) > 1:
        raise RuntimeError(f"multiple posts use slug: {slug}")
    if matches:
        return matches[0]
    for path in iter_post_paths():
        front, _ = parse_markdown(path)
        if front.get("slug") == slug:
            return path
    raise FileNotFoundError(slug)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o640)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o640)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def normalize_media_filename(filename: str | None, mime_type: str, role: str) -> str:
    if mime_type not in MEDIA_MIME_EXTENSIONS:
        raise ValueError("mime_type must be image/jpeg, image/png, image/webp, or image/gif")
    if role not in {"cover", "inline", "attachment"}:
        raise ValueError("role must be cover, inline, or attachment")
    if filename is None or not str(filename).strip():
        stem = "cover" if role == "cover" else f"media-{secrets.token_hex(8)}"
        return stem + MEDIA_MIME_EXTENSIONS[mime_type]
    raw = unicodedata.normalize("NFKC", str(filename)).strip()
    if not raw or raw in {".", ".."} or "/" in raw or "\\" in raw:
        raise ValueError("filename must be a single safe file name")
    suffix = Path(raw).suffix.lower()
    expected_mime = MEDIA_EXTENSIONS.get(suffix)
    if expected_mime != mime_type:
        raise ValueError("filename extension does not match mime_type")
    stem = Path(raw).stem
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_")[:100]
    if not stem:
        raise ValueError("filename must contain an alphanumeric name")
    normalized = f"{stem}{suffix}"
    if not MEDIA_NAME_RE.fullmatch(normalized) or normalized == "index.md":
        raise ValueError("invalid media filename")
    return normalized


def detect_media_type(content: bytes) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return ""


def decode_media(arguments: dict[str, Any]) -> tuple[bytes, str, str, str]:
    mime_type = str(arguments.get("mime_type") or "").lower().strip()
    role = str(arguments.get("role") or "inline").lower().strip()
    raw = arguments.get("data_base64")
    if not isinstance(raw, str) or not raw:
        raise ValueError("data_base64 is required")
    try:
        content = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("data_base64 is invalid") from exc
    if not content or len(content) > MEDIA_MAX_BYTES:
        raise ValueError(f"media must be between 1 byte and {MEDIA_MAX_BYTES} bytes")
    detected = detect_media_type(content)
    if detected != mime_type:
        raise ValueError("file signature does not match mime_type")
    filename = normalize_media_filename(arguments.get("filename"), mime_type, role)
    return content, mime_type, filename, role


def media_root_for_post(path: Path) -> Path:
    if not is_bundle(path):
        raise ValueError("post is not a page bundle; run hugo_migrate_post_bundle first")
    return path.parent


def media_info(path: Path, alt: str | None = None, role: str | None = None) -> dict[str, Any]:
    mime_type = MEDIA_EXTENSIONS.get(path.suffix.lower(), "application/octet-stream")
    label = alt if alt is not None else path.stem.replace("-", " ")
    return {
        "filename": path.name,
        "path": path.relative_to(SITE_ROOT).as_posix(),
        "mime_type": mime_type,
        "bytes": path.stat().st_size,
        "revision": sha256_file(path),
        "role": role or ("cover" if path.stem == "cover" else "inline"),
        "markdown": f"![{label}]({path.name})",
    }


def list_media_for_post(path: Path) -> list[dict[str, Any]]:
    root = media_root_for_post(path)
    return [
        media_info(candidate)
        for candidate in sorted(root.iterdir(), key=lambda item: item.name.lower())
        if candidate.is_file() and not candidate.is_symlink() and candidate.name != "index.md" and candidate.suffix.lower() in MEDIA_EXTENSIONS
    ]


def media_path_for_post(path: Path, filename: str) -> Path:
    root = media_root_for_post(path)
    if not isinstance(filename, str) or not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise ValueError("filename must be a single file name")
    target = (root / filename).resolve()
    if target.parent != root or target.suffix.lower() not in MEDIA_EXTENSIONS or target.name == "index.md":
        raise ValueError("invalid media filename")
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(filename)
    return target


def backup_file(path: Path, label: str) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = BACKUP_ROOT / f"{stamp}-{label}-{path.name}"
    shutil.copy2(path, target)
    return target


def audit(event: str, request_id: str | None, details: dict[str, Any]) -> None:
    record = {"time": now_iso(), "event": event, "request_id": request_id, **details}
    with AUDIT_LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def request_key(request_id: str) -> Path:
    if not isinstance(request_id, str) or not REQUEST_RE.fullmatch(request_id):
        raise ValueError("request_id is required and must be a short stable identifier")
    return IDEMPOTENCY_ROOT / f"{request_id}.json"


def remember_request(request_id: str, result: dict[str, Any]) -> None:
    atomic_write(request_key(request_id), json.dumps(result, ensure_ascii=False, indent=2) + "\n")


def previous_request(request_id: str) -> dict[str, Any] | None:
    path = request_key(request_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_site() -> dict[str, Any]:
    command = [HUGO_BIN, "--source", str(SITE_ROOT), "--destination", str(PUBLIC_ROOT)]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=BUILD_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"hugo build failed: {exc}") from exc
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout)[-4000:])
    return {"returncode": 0, "output": (completed.stdout or "")[-4000:]}


def remove_public_outputs(slug: str) -> None:
    """Remove output for a post that is no longer published.

    Hugo does not remove stale files unless the whole destination is cleaned;
    cleaning the whole destination would also remove migrated media. Remove
    only the known permalink locations instead.
    """
    for candidate in (PUBLIC_ROOT / "posts" / slug, PUBLIC_ROOT / slug):
        resolved = candidate.resolve()
        if resolved == PUBLIC_ROOT or PUBLIC_ROOT not in resolved.parents:
            continue
        if resolved.is_dir():
            shutil.rmtree(resolved)
        elif resolved.exists():
            resolved.unlink()


def list_posts(arguments: dict[str, Any]) -> dict[str, Any]:
    query = str(arguments.get("query") or "").lower().strip()
    status = arguments.get("status", "all")
    limit = max(1, min(int(arguments.get("limit", 50)), 200))
    offset = max(0, int(arguments.get("offset", 0)))
    rows = []
    for path in iter_post_paths():
        row = post_summary(path)
        if status in {"draft", "publish"} and row["status"] != status:
            continue
        if query and query not in (row["title"] + " " + row["slug"]).lower():
            continue
        rows.append(row)
    return {"posts": rows[offset : offset + limit], "total": len(rows), "offset": offset, "limit": limit}


def require_revision(path: Path, arguments: dict[str, Any]) -> str:
    expected = arguments.get("expected_revision")
    if not isinstance(expected, str) or not hmac.compare_digest(expected, sha256_file(path)):
        raise ValueError("revision mismatch; read the post again before changing it")
    return expected


def mutate(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    request_id = arguments.get("request_id")
    if not isinstance(request_id, str):
        raise ValueError("request_id is required for mutations")
    old = previous_request(request_id)
    if old is not None:
        return old

    with MUTATION_LOCK:
        if name == "hugo_create_draft":
            title = str(arguments.get("title") or "").strip()
            if not title or len(title) > 200:
                raise ValueError("title is required and must be at most 200 characters")
            slug = safe_slug(str(arguments.get("slug") or title))
            if not SLUG_RE.fullmatch(slug):
                raise ValueError("invalid slug")
            use_bundle = bool(arguments.get("bundle", True))
            path = bundle_post_path(slug) if use_bundle else post_path(slug)
            if path.exists() or post_path(slug).exists() or bundle_post_path(slug).exists():
                raise FileExistsError(slug)
            timestamp = str(arguments.get("date") or now_iso())
            front = {
                "title": title,
                "date": timestamp,
                "lastmod": timestamp,
                "slug": slug,
                "draft": True,
                "categories": arguments.get("categories") or [],
                "tags": arguments.get("tags") or [],
            }
            atomic_write(path, render_markdown(front, str(arguments.get("content") or "")))
            result = {"post": post_summary(path), "action": "created"}
            audit(name, request_id, {"slug": slug, "revision": result["post"]["revision"], "bundle": use_bundle})
        else:
            slug = str((arguments.get("post_slug") if name in {"hugo_upload_media", "hugo_delete_media"} else arguments.get("slug")) or "")
            path = find_post(slug)
            if name == "hugo_upload_media":
                root = media_root_for_post(path)
                content, mime_type, filename, role = decode_media(arguments)
                target = root / filename
                if target.exists():
                    raise FileExistsError(filename)
                atomic_write_bytes(target, content)
                alt = str(arguments.get("alt") or "").strip()[:300]
                result = {"post_slug": slug, "media": media_info(target, alt=alt or None, role=role), "action": "uploaded"}
                audit(name, request_id, {"slug": slug, "filename": filename, "bytes": len(content)})
            elif name == "hugo_delete_media":
                target = media_path_for_post(path, str(arguments.get("filename") or ""))
                expected = arguments.get("expected_revision")
                if not isinstance(expected, str) or not hmac.compare_digest(expected, sha256_file(target)):
                    raise ValueError("media revision mismatch; list the media again before deleting it")
                media_trash = TRASH_ROOT / "media"
                media_trash.mkdir(parents=True, exist_ok=True)
                stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                trash_target = media_trash / f"{stamp}-{slug}-{target.name}"
                shutil.move(str(target), str(trash_target))
                build = build_site()
                result = {"post_slug": slug, "filename": target.name, "trash_path": str(trash_target), "build": build, "action": "soft_deleted"}
                audit(name, request_id, {"slug": slug, "filename": target.name, "result": result["action"]})
            elif name == "hugo_migrate_post_bundle":
                flat = post_path(slug)
                if path != flat:
                    raise ValueError("post is already a page bundle")
                require_revision(path, arguments)
                root = bundle_root(slug)
                if root.exists():
                    raise FileExistsError(str(root))
                backup_file(path, "before-bundle-migration")
                root.mkdir(parents=True, exist_ok=False)
                shutil.move(str(path), str(root / "index.md"))
                build = build_site()
                result = {"post": post_summary(root / "index.md"), "build": build, "action": "migrated_to_bundle"}
                audit(name, request_id, {"slug": slug, "result": result["action"]})
            else:
                require_revision(path, arguments)
                front, body = parse_markdown(path)
                backup_file(path, "before-mutation")
                if name == "hugo_update_draft":
                    if "title" in arguments:
                        front["title"] = str(arguments["title"])
                    if "content" in arguments:
                        body = str(arguments["content"])
                    for key in ("categories", "tags", "summary", "date"):
                        if key in arguments:
                            front[key] = arguments[key]
                    front["lastmod"] = now_iso()
                    atomic_write(path, render_markdown(front, body))
                    result = {"post": post_summary(path), "action": "updated"}
                elif name in {"hugo_publish_post", "hugo_unpublish_post"}:
                    front["draft"] = name == "hugo_unpublish_post"
                    front["lastmod"] = now_iso()
                    atomic_write(path, render_markdown(front, body))
                    if front["draft"]:
                        remove_public_outputs(slug)
                    build = build_site()
                    result = {"post": post_summary(path), "build": build, "action": "published" if not front["draft"] else "unpublished"}
                elif name == "hugo_delete_post":
                    TRASH_ROOT.mkdir(parents=True, exist_ok=True)
                    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    source = path.parent if is_bundle(path) else path
                    target = TRASH_ROOT / f"{stamp}-{slug}" if is_bundle(path) else TRASH_ROOT / f"{stamp}-{path.name}"
                    if target.exists():
                        raise FileExistsError(str(target))
                    shutil.move(str(source), str(target))
                    remove_public_outputs(slug)
                    build = build_site()
                    result = {"slug": slug, "trash_path": str(target), "build": build, "action": "soft_deleted"}
                else:
                    raise ValueError(f"unknown mutation {name}")
                audit(name, request_id, {"slug": slug, "result": result.get("action")})
        remember_request(request_id, result)
        return result


TOOLS = [
    {"name": "hugo_list_posts", "description": "List Hugo posts and their revisions. Supports flat Markdown and Page Bundles.", "inputSchema": {"type": "object", "properties": {"status": {"type": "string", "enum": ["all", "draft", "publish"]}, "query": {"type": "string"}, "limit": {"type": "integer"}, "offset": {"type": "integer"}}}},
    {"name": "hugo_get_post", "description": "Read one Hugo post, including Page Bundle media metadata when available.", "inputSchema": {"type": "object", "required": ["slug"], "properties": {"slug": {"type": "string"}}}},
    {"name": "hugo_create_draft", "description": "Create a draft post. New drafts use a Hugo Page Bundle by default. Requires request_id.", "inputSchema": {"type": "object", "required": ["title", "request_id"], "properties": {"title": {"type": "string"}, "slug": {"type": "string"}, "content": {"type": "string"}, "categories": {"type": "array", "items": {"type": "string"}}, "tags": {"type": "array", "items": {"type": "string"}}, "bundle": {"type": "boolean", "default": True}, "request_id": {"type": "string"}}}},
    {"name": "hugo_update_draft", "description": "Update a flat post or Page Bundle with optimistic revision checking.", "inputSchema": {"type": "object", "required": ["slug", "expected_revision", "request_id"], "properties": {"slug": {"type": "string"}, "expected_revision": {"type": "string"}, "title": {"type": "string"}, "content": {"type": "string"}, "categories": {"type": "array", "items": {"type": "string"}}, "tags": {"type": "array", "items": {"type": "string"}}, "summary": {"type": "string"}, "date": {"type": "string"}, "request_id": {"type": "string"}}}},
    {"name": "hugo_publish_post", "description": "Publish a flat post or Page Bundle, rebuild Hugo, and return the new revision.", "inputSchema": {"type": "object", "required": ["slug", "expected_revision", "request_id"], "properties": {"slug": {"type": "string"}, "expected_revision": {"type": "string"}, "request_id": {"type": "string"}}}},
    {"name": "hugo_unpublish_post", "description": "Move a published post back to draft and rebuild Hugo.", "inputSchema": {"type": "object", "required": ["slug", "expected_revision", "request_id"], "properties": {"slug": {"type": "string"}, "expected_revision": {"type": "string"}, "request_id": {"type": "string"}}}},
    {"name": "hugo_delete_post", "description": "Soft-delete a post or Page Bundle into the private Hugo MCP trash and rebuild Hugo.", "inputSchema": {"type": "object", "required": ["slug", "expected_revision", "request_id"], "properties": {"slug": {"type": "string"}, "expected_revision": {"type": "string"}, "request_id": {"type": "string"}}}},
    {"name": "hugo_migrate_post_bundle", "description": "Move one legacy flat post into content/posts/<slug>/index.md without changing its slug.", "inputSchema": {"type": "object", "required": ["slug", "expected_revision", "request_id"], "properties": {"slug": {"type": "string"}, "expected_revision": {"type": "string"}, "request_id": {"type": "string"}}}},
    {"name": "hugo_upload_media", "description": "Upload a validated image into a Page Bundle and return a relative Markdown reference.", "inputSchema": {"type": "object", "required": ["post_slug", "mime_type", "data_base64", "request_id"], "properties": {"post_slug": {"type": "string"}, "filename": {"type": "string"}, "mime_type": {"type": "string", "enum": ["image/jpeg", "image/png", "image/webp", "image/gif"]}, "data_base64": {"type": "string"}, "alt": {"type": "string", "maxLength": 300}, "role": {"type": "string", "enum": ["cover", "inline", "attachment"], "default": "inline"}, "request_id": {"type": "string"}}}},
    {"name": "hugo_list_media", "description": "List image media stored beside a Page Bundle index.md.", "inputSchema": {"type": "object", "required": ["post_slug"], "properties": {"post_slug": {"type": "string"}}}},
    {"name": "hugo_delete_media", "description": "Soft-delete one Page Bundle media file after checking its media revision, then rebuild Hugo.", "inputSchema": {"type": "object", "required": ["post_slug", "filename", "expected_revision", "request_id"], "properties": {"post_slug": {"type": "string"}, "filename": {"type": "string"}, "expected_revision": {"type": "string"}, "request_id": {"type": "string"}}}},
    {"name": "hugo_build", "description": "Build the Hugo site from the current Markdown source and Page Bundles.", "inputSchema": {"type": "object", "properties": {}}},
]


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "hugo_list_posts":
        return list_posts(arguments)
    if name == "hugo_get_post":
        path = find_post(str(arguments.get("slug") or ""))
        front, body = parse_markdown(path)
        result = {"post": post_summary(path), "frontmatter": front, "content": body}
        if is_bundle(path):
            result["media"] = list_media_for_post(path)
        return result
    if name == "hugo_list_media":
        path = find_post(str(arguments.get("post_slug") or ""))
        media = list_media_for_post(path)
        return {"post": post_summary(path), "media": media, "total": len(media)}
    if name in {"hugo_create_draft", "hugo_update_draft", "hugo_publish_post", "hugo_unpublish_post", "hugo_delete_post", "hugo_migrate_post_bundle", "hugo_upload_media", "hugo_delete_media"}:
        return mutate(name, arguments)
    if name == "hugo_build":
        with MUTATION_LOCK:
            return build_site()
    raise ValueError(f"unknown tool {name}")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "HugoMCP/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.client_address[0]} {fmt % args}", flush=True)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        try:
            expected = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        actual = self.headers.get("Authorization", "")
        return bool(expected) and hmac.compare_digest(actual, f"Bearer {expected}")

    def _rate_limited(self) -> bool:
        now = time.monotonic()
        address = self.client_address[0]
        with RATE_LOCK:
            points = [point for point in RATE_STATE.get(address, []) if now - point < 60]
            if len(points) >= 30:
                RATE_STATE[address] = points
                return True
            points.append(now)
            RATE_STATE[address] = points
        return False

    def do_GET(self) -> None:
        self._send(405, {"error": error("METHOD_NOT_ALLOWED", "Use POST for MCP requests.")})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Accept")
        self.end_headers()

    def do_POST(self) -> None:
        if self.path not in {"/", "/mcp", "/index.php/action/agent-mcp"}:
            self._send(404, {"error": error("NOT_FOUND", "Unknown MCP endpoint.")})
            return
        if self._rate_limited():
            self._send(429, {"error": error("RATE_LIMITED", "Too many requests; retry later.")})
            return
        if not self._authorized():
            self._send(401, {"error": error("UNAUTHORIZED", "A Bearer token is required.")})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("request body is empty or too large")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError("JSON-RPC request must be an object")
            method = request.get("method")
            request_id = request.get("id")
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "HugoMCP", "version": "0.2.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = request.get("params") or {}
                tool_result = call_tool(str(params.get("name") or ""), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(tool_result, ensure_ascii=False)}], "structuredContent": tool_result, "isError": False}
            else:
                raise ValueError(f"unsupported method: {method}")
            self._send(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
        except FileNotFoundError as exc:
            self._send(404, {"jsonrpc": "2.0", "id": request.get("id") if "request" in locals() else None, "error": error("NOT_FOUND", str(exc))})
        except (ValueError, FileExistsError, RuntimeError, json.JSONDecodeError) as exc:
            self._send(400, {"jsonrpc": "2.0", "id": request.get("id") if "request" in locals() else None, "error": error("INVALID_REQUEST", str(exc))})
        except Exception as exc:  # keep internal details out of responses
            print(f"internal error: {exc!r}", flush=True)
            request_id = request.get("id") if isinstance(request, dict) else None
            self._send(500, {"jsonrpc": "2.0", "id": request_id, "error": error("INTERNAL_ERROR", "Unexpected server error.")})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("HUGO_MCP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HUGO_MCP_PORT", "8080")))
    args = parser.parse_args()
    ensure_roots()
    if not TOKEN_FILE.exists():
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(secrets.token_urlsafe(36) + "\n", encoding="utf-8")
        os.chmod(TOKEN_FILE, 0o600)
    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"HugoMCP listening on {args.host}:{args.port}; site={SITE_ROOT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

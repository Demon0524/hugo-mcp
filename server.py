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
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import output_build

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
    validate_roots()
    POSTS_ROOT.mkdir(parents=True, exist_ok=True)
    for path in (DATA_ROOT, TRASH_ROOT, BACKUP_ROOT, IDEMPOTENCY_ROOT):
        path.mkdir(parents=True, exist_ok=True)


def validate_roots() -> None:
    roots = (POSTS_ROOT, PUBLIC_ROOT, DATA_ROOT)
    if SITE_ROOT in roots:
        raise ValueError("content, public, and private data must not equal the site root")
    for index, root in enumerate(roots):
        for other in roots[index + 1:]:
            if root == other or root in other.parents or other in root.parents:
                raise ValueError("content, public, and private data directories must not overlap")
    if SITE_ROOT in DATA_ROOT.parents or TOKEN_FILE == SITE_ROOT or SITE_ROOT in TOKEN_FILE.parents:
        raise ValueError("private data and token must stay outside the site source tree")


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
    target = BACKUP_ROOT / f"{stamp}-{secrets.token_hex(8)}-{label}-{path.name}"
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


def build_site(*, baseline_only: bool = False) -> dict[str, Any]:
    validate_roots()
    return output_build.build_site(SITE_ROOT, PUBLIC_ROOT, DATA_ROOT, HUGO_BIN, BUILD_TIMEOUT,
                                  baseline_only=baseline_only)


def ensure_output_baseline() -> None:
    # Record the current generated paths before any source disappears.
    if not output_build.has_manifest(DATA_ROOT):
        build_site(baseline_only=True)


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


class RetryableMutationError(RuntimeError):
    def __init__(self, request_id: str, phase: str, message: str):
        super().__init__(message)
        self.request_id = request_id
        self.phase = phase


def path_revision(path: Path) -> str | None:
    """Fingerprint a whole bundle for safe move replay, including its media."""
    if path.is_symlink():
        raise ValueError("mutation paths must not be symlinks")
    if not path.exists():
        return None
    if path.is_file():
        return sha256_file(path)
    entries = []
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise ValueError("bundle must not contain symlinks")
        entries.append([child.relative_to(path).as_posix(), sha256_file(child) if child.is_file() else None])
    return hashlib.sha256(json_bytes(entries)).hexdigest()


def checked_mutation_path(value: str, root: Path) -> Path:
    path = Path(value)
    if path == root or root not in path.parents:
        raise ValueError("mutation path is outside its allowed directory")
    if any(part.is_symlink() for part in (path, *path.parents)) or path.resolve() != path:
        raise ValueError("mutation paths must not contain symlinks")
    return path


def write_step(path: Path, content: bytes) -> dict[str, Any]:
    checked_mutation_path(str(path), POSTS_ROOT)
    return {"kind": "write", "path": str(path), "before": path_revision(path),
            "after": hashlib.sha256(content).hexdigest(), "content": base64.b64encode(content).decode("ascii")}


def move_step(source: Path, target: Path, backup: Path | None = None) -> dict[str, Any]:
    checked_mutation_path(str(source), POSTS_ROOT)
    checked_mutation_path(str(target), TRASH_ROOT if TRASH_ROOT in target.parents else POSTS_ROOT)
    if target.exists():
        raise FileExistsError(str(target))
    return {"kind": "move", "path": str(source), "target": str(target),
            "before": path_revision(source), "backup": str(backup) if backup else None}


def plan_mutation(name: str, arguments: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Validate and plan without touching source, output, backups or trash."""
    needs_build = name not in {"hugo_create_draft", "hugo_update_draft", "hugo_upload_media"}
    if name == "hugo_create_draft":
        title = str(arguments.get("title") or "").strip()
        if not title or len(title) > 200:
            raise ValueError("title is required and must be at most 200 characters")
        slug = safe_slug(str(arguments.get("slug") or title))
        path = bundle_post_path(slug) if bool(arguments.get("bundle", True)) else post_path(slug)
        if post_path(slug).exists() or bundle_post_path(slug).exists():
            raise FileExistsError(slug)
        timestamp = str(arguments.get("date") or now_iso())
        front = {"title": title, "date": timestamp, "lastmod": timestamp, "slug": slug, "draft": True,
                 "categories": arguments.get("categories") or [], "tags": arguments.get("tags") or []}
        body = str(arguments.get("content") or "")
        step = write_step(path, render_markdown(front, body).encode("utf-8"))
        result = {"action": "created"}
        spec = {"post_path": str(path)}
    else:
        slug = str(arguments.get("post_slug" if name in {"hugo_upload_media", "hugo_delete_media"} else "slug") or "")
        path = find_post(slug)
        checked_mutation_path(str(path), POSTS_ROOT)
        spec = {}
        if name == "hugo_upload_media":
            content, _, filename, role = decode_media(arguments)
            target = media_root_for_post(path) / filename
            if target.exists():
                raise FileExistsError(filename)
            step = write_step(target, content)
            result = {"post_slug": slug, "action": "uploaded"}
            spec = {"media_path": str(target), "alt": str(arguments.get("alt") or "").strip()[:300] or None, "role": role}
        elif name == "hugo_delete_media":
            source = media_path_for_post(path, str(arguments.get("filename") or ""))
            require_revision(source, arguments)
            target = TRASH_ROOT / request_id / "media" / slug / source.name
            step = move_step(source, target)
            result = {"post_slug": slug, "filename": source.name, "trash_path": str(target), "action": "soft_deleted"}
        else:
            require_revision(path, arguments)
            if name == "hugo_migrate_post_bundle":
                if path != post_path(slug):
                    raise ValueError("post is already a page bundle")
                if bundle_root(slug).exists():
                    raise FileExistsError(str(bundle_root(slug)))
                target = bundle_post_path(slug)
                step = move_step(path, target, backup=path)
                result = {"action": "migrated_to_bundle"}
                spec = {"post_path": str(target)}
            elif name == "hugo_delete_post":
                source = path.parent if is_bundle(path) else path
                target = TRASH_ROOT / request_id / source.name
                step = move_step(source, target, backup=path)
                result = {"slug": slug, "trash_path": str(target), "action": "soft_deleted"}
            elif name in {"hugo_update_draft", "hugo_publish_post", "hugo_unpublish_post"}:
                front, body = parse_markdown(path)
                if name == "hugo_update_draft":
                    for key in ("title", "categories", "tags", "summary", "date"):
                        if key in arguments:
                            front[key] = arguments[key]
                    if "content" in arguments:
                        body = str(arguments["content"])
                    action = "updated"
                else:
                    front["draft"] = name == "hugo_unpublish_post"
                    action = "unpublished" if front["draft"] else "published"
                front["lastmod"] = now_iso()
                step = write_step(path, render_markdown(front, body).encode("utf-8"))
                result = {"action": action}
                spec = {"post_path": str(path)}
            else:
                raise ValueError(f"unknown mutation {name}")
    return {"slug": slug, "step": step, "result": result, "spec": spec, "build_required": needs_build}


def apply_step(step: dict[str, Any], verify_only: bool = False) -> None:
    path = checked_mutation_path(step["path"], POSTS_ROOT)
    current = path_revision(path)
    if step["kind"] == "write":
        if current == step["after"]:
            return
        if verify_only or current != step["before"]:
            raise ValueError("source changed during pending operation; inspect before resuming")
        if path.exists():
            backup_file(path, "before-mutation")
        atomic_write_bytes(path, base64.b64decode(step["content"], validate=True))
    else:
        target = Path(step["target"])
        checked_mutation_path(str(target), TRASH_ROOT if TRASH_ROOT in target.parents else POSTS_ROOT)
        target_revision = path_revision(target)
        if current is None and target_revision == step["before"]:
            return
        if verify_only or current != step["before"] or target_revision is not None:
            raise ValueError("move paths changed during pending operation; inspect source and trash before resuming")
        if step.get("backup"):
            backup_file(checked_mutation_path(step["backup"], POSTS_ROOT), "before-mutation")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))


def materialize_result(plan: dict[str, Any]) -> dict[str, Any]:
    result = dict(plan["result"])
    spec = plan["spec"]
    if "post_path" in spec:
        result["post"] = post_summary(Path(spec["post_path"]))
    if "media_path" in spec:
        result["media"] = media_info(Path(spec["media_path"]), alt=spec["alt"], role=spec["role"])
    return result


def ensure_no_pending_request() -> None:
    for path in sorted(IDEMPOTENCY_ROOT.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("journal_version") == 2 and record.get("state") == "pending":
            raise RetryableMutationError(path.stem, record["phase"],
                                         f"pending operation {path.stem}; resume it with the same arguments and request_id first")


def resume_mutation(request_id: str, record: dict[str, Any]) -> dict[str, Any]:
    try:
        for plan in record["plans"]:
            apply_step(plan["step"], verify_only=record["phase"] != "source")
        if record["phase"] == "source":
            results = [materialize_result(plan) for plan in record["plans"]]
            record["result"] = {"action": "batch_completed", "items": results} if record["batch"] else results[0]
            record["phase"] = "build" if record["build_required"] else "finish"
            remember_request(request_id, record)
        if record["phase"] == "build":
            record["result"]["build"] = build_site()
            record["phase"] = "finish"
            remember_request(request_id, record)
        audit(record["name"], request_id, {"slugs": [plan["slug"] for plan in record["plans"]],
                                          "result": record["result"]["action"]})
        record["state"] = "complete"
        # Completed journals no longer need the private source payloads.
        record.pop("plans")
        remember_request(request_id, record)
        return record["result"]
    except Exception as exc:
        raise RetryableMutationError(request_id, record["phase"],
                                     f"operation pending; retry identical arguments and request_id after resolving the error: {exc}") from exc


def batch_plans(arguments: dict[str, Any], request_id: str) -> list[dict[str, Any]]:
    items = arguments.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise ValueError("items must contain between 1 and 50 explicit post operations")
    names = {"publish": "hugo_publish_post", "unpublish": "hugo_unpublish_post", "delete": "hugo_delete_post"}
    plans = []
    paths = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"action", "slug", "expected_revision"}:
            raise ValueError("each batch item must contain only action, slug, and expected_revision")
        if not all(isinstance(value, str) for value in item.values()):
            raise ValueError("batch action, slug, and expected_revision must be strings")
        if item["action"] not in names:
            raise ValueError("batch action must be publish, unpublish, or delete")
        plan = plan_mutation(names[item["action"]], item, request_id)
        identity = find_post(plan["slug"])
        if identity in paths:
            raise ValueError("each post may occur only once in a batch")
        paths.add(identity)
        plans.append(plan)
    return plans


def mutate(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    with MUTATION_LOCK:
        batch = name == "hugo_batch_posts"
        if batch and not isinstance(arguments.get("dry_run", True), bool):
            raise ValueError("dry_run must be a boolean")
        if batch and arguments.get("dry_run", True):
            plans = batch_plans(arguments, "preview")
            return {"action": "planned", "build_required": True,
                    "items": [{"slug": plan["slug"], "action": item["action"],
                               "expected_revision": item["expected_revision"]} for plan, item in zip(plans, arguments["items"])]}
        request_id = arguments.get("request_id")
        request_key(request_id)
        fingerprint = hashlib.sha256(json.dumps({"name": name, "arguments": arguments}, sort_keys=True,
                                               ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        old = previous_request(request_id)
        if old is not None:
            if old.get("journal_version") != 2:
                return old  # Preserve pre-journal idempotency records.
            if old["fingerprint"] != fingerprint:
                raise ValueError("request_id was already used with different arguments or tool")
            if old["state"] == "complete":
                return old["result"]
            return resume_mutation(request_id, old)
        ensure_no_pending_request()
        plans = batch_plans(arguments, request_id) if batch else [plan_mutation(name, arguments, request_id)]
        needs_build = any(plan["build_required"] for plan in plans)
        if needs_build:
            ensure_output_baseline()
        record = {"journal_version": 2, "fingerprint": fingerprint, "name": name, "state": "pending",
                  "phase": "source", "batch": batch, "build_required": needs_build, "plans": plans}
        remember_request(request_id, record)
        return resume_mutation(request_id, record)


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
    {"name": "hugo_batch_posts", "description": "Preview (default) or execute up to 50 explicit publish, unpublish, or soft-delete operations. Validate every revision before any write, then build once after output tracking is initialized. Execution requires request_id; retry identical inputs to resume pending work.", "inputSchema": {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "object", "required": ["action", "slug", "expected_revision"], "additionalProperties": False, "properties": {"action": {"type": "string", "enum": ["publish", "unpublish", "delete"]}, "slug": {"type": "string"}, "expected_revision": {"type": "string"}}}}, "dry_run": {"type": "boolean", "default": True}, "request_id": {"type": "string"}}, "allOf": [{"if": {"required": ["dry_run"], "properties": {"dry_run": {"const": False}}}, "then": {"required": ["request_id"]}}]}},
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
    if name in {"hugo_create_draft", "hugo_update_draft", "hugo_publish_post", "hugo_unpublish_post", "hugo_delete_post", "hugo_migrate_post_bundle", "hugo_upload_media", "hugo_delete_media", "hugo_batch_posts"}:
        return mutate(name, arguments)
    if name == "hugo_build":
        with MUTATION_LOCK:
            ensure_no_pending_request()
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
        if status == 429:
            self.send_header("Retry-After", "60")
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
            self._send(429, {"error": {**error("RATE_LIMITED", "Too many requests; retry later."), "retryable": True}})
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
        except RetryableMutationError as exc:
            details = {"request_id": exc.request_id, "phase": exc.phase}
            self._send(409, {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                             "error": {**error("OPERATION_PENDING", str(exc), details), "retryable": True}})
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

#!/usr/bin/env python3
"""Small, filesystem-backed MCP server for a Hugo site.

The site content is the source of truth. Mutations are atomic, guarded by a
revision hash, recorded in an audit log, and require a request_id for
idempotency. Deletes are soft-deletes into a private trash directory.
"""

from __future__ import annotations

import argparse
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
MAX_BODY = int(os.environ.get("HUGO_MCP_MAX_BODY", str(2 * 1024 * 1024)))
BUILD_TIMEOUT = int(os.environ.get("HUGO_BUILD_TIMEOUT", "120"))

SITE_ROOT_STR = str(SITE_ROOT)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,120}$")
REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,120}$")
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


def post_summary(path: Path) -> dict[str, Any]:
    front, body = parse_markdown(path)
    slug = str(front.get("slug") or path.stem)
    return {
        "slug": slug,
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
    if direct.exists():
        return direct
    for path in POSTS_ROOT.glob("*.md"):
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
    for path in sorted(POSTS_ROOT.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True):
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
            path = post_path(slug)
            if path.exists():
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
            audit(name, request_id, {"slug": slug, "revision": result["post"]["revision"]})
        else:
            slug = str(arguments.get("slug") or "")
            path = find_post(slug)
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
                target = TRASH_ROOT / f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{path.name}"
                shutil.move(str(path), str(target))
                remove_public_outputs(slug)
                build = build_site()
                result = {"slug": slug, "trash_path": str(target), "build": build, "action": "soft_deleted"}
            else:
                raise ValueError(f"unknown mutation {name}")
            audit(name, request_id, {"slug": slug, "result": result.get("action")})
        remember_request(request_id, result)
        return result


TOOLS = [
    {"name": "hugo_list_posts", "description": "List Hugo posts and their revisions.", "inputSchema": {"type": "object", "properties": {"status": {"type": "string", "enum": ["all", "draft", "publish"]}, "query": {"type": "string"}, "limit": {"type": "integer"}, "offset": {"type": "integer"}}}},
    {"name": "hugo_get_post", "description": "Read one Hugo Markdown post.", "inputSchema": {"type": "object", "required": ["slug"], "properties": {"slug": {"type": "string"}}}},
    {"name": "hugo_create_draft", "description": "Create a draft Markdown post. Requires request_id.", "inputSchema": {"type": "object", "required": ["title", "request_id"], "properties": {"title": {"type": "string"}, "slug": {"type": "string"}, "content": {"type": "string"}, "categories": {"type": "array", "items": {"type": "string"}}, "tags": {"type": "array", "items": {"type": "string"}}, "request_id": {"type": "string"}}}},
    {"name": "hugo_update_draft", "description": "Update a post with optimistic revision checking.", "inputSchema": {"type": "object", "required": ["slug", "expected_revision", "request_id"], "properties": {"slug": {"type": "string"}, "expected_revision": {"type": "string"}, "title": {"type": "string"}, "content": {"type": "string"}, "categories": {"type": "array", "items": {"type": "string"}}, "tags": {"type": "array", "items": {"type": "string"}}, "request_id": {"type": "string"}}}},
    {"name": "hugo_publish_post", "description": "Publish a post, rebuild Hugo, and return the new revision.", "inputSchema": {"type": "object", "required": ["slug", "expected_revision", "request_id"], "properties": {"slug": {"type": "string"}, "expected_revision": {"type": "string"}, "request_id": {"type": "string"}}}},
    {"name": "hugo_unpublish_post", "description": "Move a published post back to draft and rebuild Hugo.", "inputSchema": {"type": "object", "required": ["slug", "expected_revision", "request_id"], "properties": {"slug": {"type": "string"}, "expected_revision": {"type": "string"}, "request_id": {"type": "string"}}}},
    {"name": "hugo_delete_post", "description": "Soft-delete a post into the private Hugo MCP trash and rebuild Hugo.", "inputSchema": {"type": "object", "required": ["slug", "expected_revision", "request_id"], "properties": {"slug": {"type": "string"}, "expected_revision": {"type": "string"}, "request_id": {"type": "string"}}}},
    {"name": "hugo_build", "description": "Build the Hugo site from the current Markdown source.", "inputSchema": {"type": "object", "properties": {}}},
]


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "hugo_list_posts":
        return list_posts(arguments)
    if name == "hugo_get_post":
        path = find_post(str(arguments.get("slug") or ""))
        front, body = parse_markdown(path)
        return {"post": post_summary(path), "frontmatter": front, "content": body}
    if name in {"hugo_create_draft", "hugo_update_draft", "hugo_publish_post", "hugo_unpublish_post", "hugo_delete_post"}:
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
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "HugoMCP", "version": "0.1.0"}}
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

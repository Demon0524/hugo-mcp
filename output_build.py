"""Build privately and reconcile only outputs this service has generated."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath


def manifest_path(data_root: Path) -> Path:
    return data_root / "output-manifest.json"


def has_manifest(data_root: Path) -> bool:
    path = manifest_path(data_root)
    if path.is_symlink():
        raise ValueError("output manifest must not be a symlink")
    return path.is_file()


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid generated output path")
    path = PurePosixPath(value)
    if (not value or "\\" in value or path.is_absolute()
            or ".." in path.parts or str(path) != value or value == "."):
        raise ValueError("invalid generated output path")
    return value


def _atomic_json(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise ValueError("output state must not be a symlink")
    descriptor, temporary = tempfile.mkstemp(prefix=".output-state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_state(path: Path, identity: dict, pending: bool = False) -> dict:
    if path.is_symlink():
        raise ValueError("output state must not be a symlink")
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(data.get(key) != value for key, value in identity.items()):
        raise ValueError("output state belongs to a different site or format")
    files = data.get("files")
    if not isinstance(files, dict):
        raise ValueError("invalid output state")
    result = {}
    for relative, recorded in files.items():
        _relative(relative)
        digests = recorded if pending else [recorded]
        if (not isinstance(digests, list) or not digests
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in digests)):
            raise ValueError("invalid output digest")
        result[relative] = set(digests)
    return result


def _scan_generated(root: Path) -> dict:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("generated output must be a regular directory")

    def scan_error(error):
        raise error

    files = {}
    for directory, directories, filenames in os.walk(root, followlinks=False, onerror=scan_error):
        for name in directories + filenames:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError("generated output must not contain symlinks")
        for name in filenames:
            path = Path(directory) / name
            if not path.is_file():
                raise ValueError("generated output must contain regular files only")
            files[_relative(path.relative_to(root).as_posix())] = _digest(path)
    return files


def _check_path(root: Path, relative: str) -> Path:
    current = root
    if root.is_symlink():
        raise ValueError("public output must not contain symlinks")
    for part in PurePosixPath(_relative(relative)).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("public output must not contain symlinks")
    return current


def _copy_output(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".hugo-mcp-", dir=target.parent)
    os.close(descriptor)
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, source.stat().st_mode & 0o777)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _establish_baseline(stage: Path, public_root: Path, data_root: Path, identity: dict) -> dict:
    generated = _scan_generated(stage)
    existing = _read_state(manifest_path(data_root), identity)
    _read_state(data_root / "output-pending.json", identity, pending=True)
    if has_manifest(data_root):
        return {"baseline_only": True, "tracked_count": len(existing)}

    # Adopt only current public bytes. Rendering the pre-mutation source here
    # must not publish changes that the user is about to withdraw or delete.
    tracked = {}
    for relative in generated:
        target = _check_path(public_root, relative)
        if target.is_file():
            tracked[relative] = _digest(target)
    _atomic_json(manifest_path(data_root), {**identity, "files": tracked})
    return {"baseline_only": True, "tracked_count": len(tracked)}


def _reconcile(stage: Path, public_root: Path, data_root: Path, identity: dict) -> dict:
    generated = _scan_generated(stage)
    owned = _read_state(manifest_path(data_root), identity)
    pending_path = data_root / "output-pending.json"
    for relative, digests in _read_state(pending_path, identity, pending=True).items():
        owned.setdefault(relative, set()).update(digests)

    removable = set()
    preserved = []
    for relative, digests in owned.items():
        target = _check_path(public_root, relative)
        if relative in generated or not target.exists():
            continue
        if target.is_file() and _digest(target) in digests:
            removable.add(relative)
        else:
            preserved.append(relative)

    # Check every structural conflict before changing any public output.
    for relative in generated:
        target = _check_path(public_root, relative)
        for parent in target.parents:
            if parent == public_root:
                break
            if parent.exists() and not parent.is_dir():
                if parent.relative_to(public_root).as_posix() not in removable:
                    raise ValueError("generated output conflicts with an unmanaged file")
        if target.is_dir():
            descendants = list(target.rglob("*"))
            managed_directories = set()
            for old in removable:
                managed_directories.update((public_root / old).parents)
            if target not in managed_directories:
                raise ValueError("generated output conflicts with an unmanaged directory")
            for child in descendants:
                _check_path(public_root, child.relative_to(public_root).as_posix())
                if child.is_dir():
                    if child not in managed_directories:
                        raise ValueError("generated output conflicts with an unmanaged directory")
                    continue
                if child.relative_to(public_root).as_posix() not in removable:
                    raise ValueError("generated output conflicts with an unmanaged directory")
        elif target.exists() and not target.is_file():
            raise ValueError("public output must contain regular files only")

    # Keep both old and new fingerprints until reconciliation completes. A retry
    # can then remove stale files even after a previous attempt partly copied them.
    pending = {relative: set(digests) for relative, digests in owned.items()}
    for relative, digest in generated.items():
        pending.setdefault(relative, set()).add(digest)
    _atomic_json(pending_path, {**identity, "files": {
        relative: sorted(digests) for relative, digests in pending.items()
    }})

    public_root.mkdir(parents=True, exist_ok=True)
    for relative in sorted(removable, key=lambda item: (-item.count("/"), item)):
        target = _check_path(public_root, relative)
        # An external writer may have changed this file after preflight.
        if not target.is_file() or _digest(target) not in owned[relative]:
            preserved.append(relative)
            continue
        target.unlink()
        parent = target.parent
        while parent != public_root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    for relative in sorted(generated):
        target = _check_path(public_root, relative)
        if target.is_dir():
            # Only empty directories can remain after verified owned removals.
            for directory in sorted(target.rglob("*"), key=lambda path: -len(path.parts)):
                if directory.is_symlink() or not directory.is_dir():
                    raise ValueError("public output changed during reconciliation")
                directory.rmdir()
            target.rmdir()
        _copy_output(stage / relative, target)

    _atomic_json(manifest_path(data_root), {**identity, "files": generated})
    pending_path.unlink()
    return {
        "generated_count": len(generated),
        "removed_count": len(removable) - len(set(preserved) & removable),
        "preserved_modified": sorted(set(preserved)),
        "warnings": (["Externally modified obsolete outputs were preserved; review preserved_modified."]
                     if preserved else []),
    }


def build_site(site_root: Path, public_root: Path, data_root: Path,
               hugo_bin: str, timeout: int, *, baseline_only: bool = False) -> dict:
    """Build without touching public on Hugo failure; resume interrupted syncs.

    The caller must serialize builds and mutations. The initial successful build
    establishes ownership; unknown pre-existing files are never garbage-collected.
    Output files are replaced individually, not as an atomic whole-site swap.
    With baseline_only, adopt current matching public paths without changing public.
    """
    site_root = Path(site_root).resolve()
    public_root = Path(public_root).absolute()
    data_root = Path(data_root).resolve()
    if public_root == site_root or site_root not in public_root.parents:
        raise ValueError("public output must be a directory within the site")
    _check_path(site_root, public_root.relative_to(site_root).as_posix())
    if public_root.exists() and not public_root.is_dir():
        raise ValueError("public output must be a directory")
    if data_root == public_root or public_root in data_root.parents:
        raise ValueError("private build data must not be inside public output")
    if data_root == site_root or site_root in data_root.parents or data_root in site_root.parents:
        raise ValueError("private build data must be separate from the site source tree")
    data_root.mkdir(parents=True, exist_ok=True)
    identity = {"version": 1, "site_root": str(site_root), "public_root": str(public_root)}
    with tempfile.TemporaryDirectory(prefix="hugo-output-", dir=data_root) as directory:
        stage = Path(directory) / "public"
        stage.mkdir()
        completed = subprocess.run(
            [hugo_bin, "--source", str(site_root), "--destination", str(stage)],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "Hugo build failed")[-4000:])
        if baseline_only:
            result = _establish_baseline(stage, public_root, data_root, identity)
        else:
            result = _reconcile(stage, public_root, data_root, identity)
        return {"returncode": 0, "output": completed.stdout[-4000:], **result}

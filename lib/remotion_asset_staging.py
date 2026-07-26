"""Stage local media into a project-scoped Remotion public dir for renders.

Headless Chromium blocks ``file://`` URIs for ``<Audio>`` (and can be flaky for
other media). Remotion's ``staticFile()`` only serves paths under a public
directory (default ``remotion-composer/public/``, or an explicit ``--public-dir``).

This module stages into a **render-scoped** public directory under
``projects/<id>/remotion-public-<render_id>/`` (never the shared composer tree),
rewrites props to relative ``staticFile()`` paths, and supports reliable cleanup
after render while leaving a debug report in the project workspace. Each render
gets a unique directory so concurrent renders in the same project never collide,
and cleanup only ever removes the directory this invocation created.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import shutil
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

_REMOTE_PREFIXES = ("http://", "https://", "data:")
_RESERVED_SLUGS = frozenset({".", ".."})
# Dots are disallowed so metadata project_id=".." cannot escape via Path join.
_SLUG_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")
# Render-scoped staging dirs we create: remotion-public-<hex> / .remotion-public-<hex>.
# Cleanup only ever removes a dir whose name matches this contract, so a concurrent
# render's dir (different id) and any pre-existing dir are never touched.
_STAGING_DIR_RE = re.compile(r"^\.(?:remotion-public)-[0-9a-f]{4,}$|^remotion-public-[0-9a-f]{4,}$")


def derive_staging_slug(output_path: Path, composition_data: dict[str, Any] | None = None) -> str:
    """Derive a stable project slug for naming / reports (never a path segment from raw metadata)."""
    composition_data = composition_data or {}
    meta = composition_data.get("metadata") or {}
    for key in ("project_id", "project_slug", "slug"):
        raw = meta.get(key)
        if raw:
            return _sanitize_slug(str(raw))

    resolved = output_path.resolve()
    parts = resolved.parts
    if "projects" in parts:
        idx = parts.index("projects")
        if idx + 1 < len(parts):
            return _sanitize_slug(parts[idx + 1])

    return _sanitize_slug(resolved.parent.name or "remotion-staged")


def _sanitize_slug(value: str) -> str:
    """Return a path-safe slug; reject reserved dot segments (``.``, ``..``)."""
    cleaned = _SLUG_SAFE.sub("-", value.strip()).strip("-_")
    if not cleaned or cleaned in _RESERVED_SLUGS:
        return "remotion-staged"
    # Reject any residual reserved segment if separators somehow survived.
    for part in cleaned.replace("\\", "/").split("/"):
        if part in _RESERVED_SLUGS or part == "":
            return "remotion-staged"
    return cleaned


def resolve_project_public_dir(
    output_path: Path,
    composition_data: dict[str, Any] | None = None,
    *,
    render_id: str | None = None,
) -> Path:
    """Resolve a render-scoped Remotion ``--public-dir`` (not remotion-composer/public).

    Each render gets a **unique** staging directory so concurrent renders in the
    same project never share or overwrite each other's staged media, and cleanup
    can only ever delete the directory this invocation created. The directory
    name carries a short random id (``remotion-public-<hex>`` under a project
    tree, ``.remotion-public-<hex>`` otherwise); cleanup refuses to remove any
    directory whose name does not match this contract.

    Prefer ``projects/<slug>/remotion-public-<render_id>/`` when the output lives
    under a project tree; otherwise use ``<output_parent>/.remotion-public-<render_id>/``.
    """
    del composition_data  # reserved for future metadata overrides
    rid = render_id or secrets.token_hex(4)
    resolved = output_path.resolve()
    parts = resolved.parts
    if "projects" in parts:
        idx = parts.index("projects")
        slug_idx = idx + 1
        if slug_idx < len(parts):
            # Climb from the output file up to projects/<slug>/.
            # parents[0] = one level up; we need (depth(file) - depth(slug)) - 1.
            levels_to_slug = (len(parts) - 1) - slug_idx
            project_dir = resolved.parents[levels_to_slug - 1] if levels_to_slug >= 1 else resolved.parent
            return project_dir / f"remotion-public-{rid}"
    return resolved.parent / f".remotion-public-{rid}"


def ensure_contained(path: Path, root: Path) -> Path:
    """Resolve *path* and raise if it is not under *root*."""
    resolved = path.resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(
            f"staging path escapes root: {resolved} is not under {root_resolved}"
        ) from exc
    return resolved


def cleanup_staging_dir(public_dir: Path) -> None:
    """Remove a render-scoped Remotion public staging directory after render.

    Only deletes the directory if its name matches the render-scoped staging
    contract (``remotion-public-<hex>`` / ``.remotion-public-<hex>``). Because
    each render creates a uniquely-named directory, this never touches a
    concurrent render's directory or a pre-existing directory.
    """
    if not public_dir.exists():
        return
    if not _STAGING_DIR_RE.match(public_dir.name):
        return
    shutil.rmtree(public_dir, ignore_errors=True)


def _mirror_public_dir(source_root: Path, staging_root: Path) -> None:
    """Make the render-scoped staging dir also serve pre-existing public/ assets.

    ``--public-dir`` fully replaces Remotion's default public dir for the render,
    so anything staticFile()'d from the real ``remotion-composer/public/`` tree
    (demo-props fixtures, and — per SCENE_TYPES.md — pipeline-staged assets like
    ``anime_scene.images[]`` / ``screenshot_scene.backgroundImage``, which are
    documented as public/-relative paths copied there by the asset-director stage)
    would otherwise 404 under the isolated staging dir. Symlink each top-level
    entry in (read-only, nothing is written back to *source_root*); fall back to
    a copy where symlinks aren't permitted (e.g. Windows without dev mode).
    """
    if not source_root.is_dir():
        return
    for entry in source_root.iterdir():
        link = staging_root / entry.name
        if link.exists() or link.is_symlink():
            continue
        try:
            link.symlink_to(entry, target_is_directory=entry.is_dir())
        except OSError:
            try:
                if entry.is_dir():
                    shutil.copytree(entry, link)
                else:
                    shutil.copy2(entry, link)
            except OSError:
                pass


def _is_remote_asset(src: str) -> bool:
    return src.startswith(_REMOTE_PREFIXES)


def _looks_like_windows_drive(path_text: str) -> bool:
    """True for ``C:\\...``, ``C:/...``, or ``/C:/...`` drive paths."""
    text = path_text.replace("\\", "/")
    if text.startswith("/") and len(text) >= 3 and text[1].isalpha() and text[2] == ":":
        text = text[1:]
    return len(text) >= 2 and text[0].isalpha() and text[1] == ":"


def _as_filesystem_path(path_text: str) -> Path:
    """Build a ``Path`` from URI-derived text, normalizing Windows drive forms."""
    if _looks_like_windows_drive(path_text):
        # PureWindowsPath keeps drive + separators; as_posix() makes .name
        # correct on POSIX hosts (where ``\\`` is not a separator).
        text = path_text.replace("\\", "/")
        if text.startswith("/") and len(text) >= 3 and text[1].isalpha() and text[2] == ":":
            text = text[1:]
        return Path(PureWindowsPath(text).as_posix())
    return Path(path_text)


def _parse_file_uri(uri: str) -> Path | None:
    """Parse a ``file:`` URI into a filesystem ``Path`` (no existence check).

    Handles common forms agents and tooling produce:

    - POSIX: ``file:///Users/me/voice.mp3``
    - Windows drive (RFC-ish): ``file:///C:/Users/me/voice.mp3``
    - Windows drive as authority: ``file://C:/Users/me/voice.mp3``
    - Windows drive (naive ``f"file://{path}"``): ``file://C:\\Users\\me\\voice.mp3``
    """
    if not uri.lower().startswith("file:"):
        return None

    parsed = urlparse(uri)
    if parsed.scheme.lower() != "file":
        return None

    netloc = unquote(parsed.netloc or "")
    path_part = unquote(parsed.path or "")

    if netloc and netloc.lower() not in ("localhost",):
        # Naive Windows URI file://C:\Users\... — urlparse (esp. on POSIX)
        # may stuff the entire path into netloc with an empty path.
        if not path_part and ("\\" in netloc or (len(netloc) >= 2 and netloc[1] in ":|")):
            drive_path = (
                netloc.replace("|", ":", 1)
                if len(netloc) >= 2 and netloc[1] == "|"
                else netloc
            )
            if _looks_like_windows_drive(drive_path):
                return _as_filesystem_path(drive_path)

        # Windows drive as authority: netloc="C:", path="/Users/..." or "\Users\..."
        if len(netloc) == 2 and netloc[1] == ":":
            return _as_filesystem_path(netloc + path_part)
        if len(netloc) == 2 and netloc[1] == "|":
            return _as_filesystem_path(netloc[0] + ":" + path_part)
        return Path(f"//{netloc}{path_part}")

    if not path_part:
        return None
    try:
        converted = url2pathname(path_part)
    except (OSError, ValueError):
        converted = path_part
    return _as_filesystem_path(converted)


def _file_uri_to_path(uri: str) -> Path | None:
    """Convert a ``file://`` URI to an existing filesystem path, or None."""
    candidate = _parse_file_uri(uri)
    if candidate is None:
        return None
    try:
        return candidate.resolve() if candidate.exists() else None
    except OSError:
        return None


def _resolve_local_path(src: str) -> Path | None:
    """Return a filesystem path when *src* refers to local media."""
    if not src or _is_remote_asset(src):
        return None

    if src.lower().startswith("file:"):
        return _file_uri_to_path(src)

    path = Path(src)
    if path.is_absolute():
        return path.resolve() if path.exists() else None

    # Already a public-relative path (e.g. "narration.mp3") — do not treat as
    # a filesystem path unless it exists relative to cwd.
    if "/" in src and not path.exists():
        return None

    resolved = path.resolve()
    return resolved if resolved.exists() else None


def _stage_file(src: Path, staging_dir: Path, *, staging_root: Path) -> Path:
    """Copy *src* into *staging_dir*, disambiguating basename collisions.

    Destination is verified to remain under *staging_root* after resolve.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    ensure_contained(staging_dir, staging_root)

    dest = staging_dir / src.name
    ensure_contained(dest, staging_root)

    if dest.exists():
        try:
            if dest.resolve() == src.resolve():
                return ensure_contained(dest, staging_root)
        except OSError:
            pass
        digest = hashlib.sha256(str(src.resolve()).encode()).hexdigest()[:8]
        dest = staging_dir / f"{src.stem}_{digest}{src.suffix}"
        ensure_contained(dest, staging_root)

    shutil.copy2(src, dest)
    return ensure_contained(dest, staging_root)


def _set_nested(props: dict[str, Any], key: str, value: str) -> None:
    if key == "audio.narration.src":
        props.setdefault("audio", {}).setdefault("narration", {})["src"] = value
    elif key == "audio.music.src":
        props.setdefault("audio", {}).setdefault("music", {})["src"] = value
    elif key.startswith("cuts."):
        _, idx_s, field = key.split(".", 2)
        props["cuts"][int(idx_s)][field] = value


def _collect_staging_targets(props: dict[str, Any]) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for i, cut in enumerate(props.get("cuts") or []):
        source = cut.get("source")
        if source:
            targets.append((f"cuts.{i}.source", str(source)))

    audio = props.get("audio") or {}
    narration = audio.get("narration")
    if isinstance(narration, dict) and narration.get("src"):
        targets.append(("audio.narration.src", str(narration["src"])))

    music = audio.get("music")
    if isinstance(music, dict) and music.get("src"):
        targets.append(("audio.music.src", str(music["src"])))

    return targets


def stage_local_assets_for_remotion(
    props: dict[str, Any],
    *,
    public_dir: Path,
    project_slug: str | None = None,
    mirror_from: Path | None = None,
) -> dict[str, Any]:
    """Stage local media into *public_dir* and rewrite props for ``staticFile()``.

    *public_dir* is the Remotion ``--public-dir`` root (project-scoped). Files are
    copied directly into that root; props get basename-relative paths
    (``narration.mp3``), not shared ``remotion-composer/public/<slug>/`` paths.

    *mirror_from*, if given, is Remotion's real default public dir (e.g.
    ``remotion-composer/public/``). Its top-level entries are linked (read-only)
    into *public_dir* first, so composition fields this module doesn't stage
    directly (``backgroundImage``, ``anime_scene.images[]``,
    ``screenshot_scene.backgroundImage``, fixtures) still resolve — see
    :func:`_mirror_public_dir`.

    Mutates *props* in place. Returns a report dict for render metadata / debugging.
    """
    slug = _sanitize_slug(project_slug) if project_slug else "remotion-staged"
    staging_root = public_dir.resolve()
    staging_root.mkdir(parents=True, exist_ok=True)
    ensure_contained(staging_root, staging_root)
    if mirror_from is not None:
        _mirror_public_dir(mirror_from.resolve(), staging_root)

    staged: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    already_staged: dict[str, str] = {}

    for key, src in _collect_staging_targets(props):
        if _is_remote_asset(src):
            skipped.append({"key": key, "src": src, "reason": "remote"})
            continue

        local = _resolve_local_path(src)
        if local is None:
            skipped.append({"key": key, "src": src, "reason": "not_local_or_missing"})
            continue

        src_key = str(local.resolve())
        if src_key in already_staged:
            relative = already_staged[src_key]
        else:
            dest = _stage_file(local, staging_root, staging_root=staging_root)
            relative = dest.name
            already_staged[src_key] = relative

        _set_nested(props, key, relative)
        staged.append({"key": key, "from": str(local), "to": relative})

    return {
        "project_slug": slug,
        "public_dir": str(staging_root),
        "staging_dir": str(staging_root),
        "staged": staged,
        "skipped": skipped,
        "lifecycle": "render-scoped unique dir; caller should cleanup_staging_dir after render",
    }

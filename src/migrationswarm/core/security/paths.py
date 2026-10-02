"""Cross-platform path validation for untrusted relative paths."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath


class PathSafetyError(ValueError):
    """Raised when a path is absolute, protected, traversing, or escapes a root."""


_PROTECTED = {".git", ".migrationswarm"}


def normalize_relative_path(value: str | Path) -> str:
    """Return a safe POSIX relative path, rejecting Windows and POSIX escapes."""
    raw = str(value).strip()
    if not raw or "\x00" in raw:
        raise PathSafetyError("path must be non-empty and contain no NUL bytes")
    if raw.startswith(("/", "\\", "//", "\\\\")):
        raise PathSafetyError(f"Absolute or UNC path is not allowed: {value}")
    windows = PureWindowsPath(raw)
    if windows.is_absolute() or bool(windows.drive) or windows.anchor:
        raise PathSafetyError(f"Absolute or drive-qualified path is not allowed: {value}")
    normalized = raw.replace("\\", "/")
    if re.match(r"^[A-Za-z]:", normalized):
        raise PathSafetyError(f"Drive-qualified path is not allowed: {value}")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".."} for part in path.parts):
        raise PathSafetyError(f"path traversal is not allowed: {value}")
    if any(part.casefold() in _PROTECTED for part in path.parts):
        raise PathSafetyError(f"Protected path is not allowed: {value}")
    return path.as_posix()


def ensure_contained(
    root: str | Path, candidate: str | Path, *, reject_symlinks: bool = True
) -> Path:
    """Resolve a candidate and ensure it remains inside root."""
    root_path = Path(root).expanduser().resolve()
    candidate_path = Path(candidate).expanduser()
    if reject_symlinks:
        _reject_symlink_components(root_path, candidate_path)
    resolved = candidate_path.resolve(strict=False)
    try:
        resolved.relative_to(root_path)
    except ValueError as error:
        raise PathSafetyError(f"path escapes root: {candidate}") from error
    return resolved


def safe_join(root: str | Path, relative: str | Path) -> Path:
    """Join a user/model supplied relative path below root."""
    normalized = normalize_relative_path(relative)
    root_path = Path(root).expanduser().resolve()
    candidate = root_path.joinpath(*PurePosixPath(normalized).parts)
    return ensure_contained(root_path, candidate)


def _reject_symlink_components(root: Path, candidate: Path) -> None:
    """Reject symlink components so a later filesystem change cannot redirect writes."""
    try:
        relative = candidate.absolute().relative_to(root.absolute())
    except ValueError:
        return
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise PathSafetyError(f"symlink path component is not allowed: {current}")

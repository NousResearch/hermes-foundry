"""Identity-scoped durable Hermes state synchronization.

Foundry persists ``$HOME`` and ``/files`` only inside one hosted-agent session.  This module stores
the deliberately durable subset of ``HERMES_HOME`` in the Agent User's OneDrive App Folder, so a
new Foundry session for the same Agent User and its verified owner can restore memories and learned
skills. The gateway invokes it only for owner turns and owner-authorized routines. The Graph token
is supplied by the active agent turn and is never retained after the call.

Packaged defaults and sparse owner overrides are separate. During the release-80 compatibility
window, ``$HERMES_HOME/state-sync.json`` remains as a materialized legacy view. A hard denylist
remains in code: configuration can add new durable Hermes paths, but it can never opt credentials,
runtime code, conversation databases, locks, caches, or logs into the remote archive.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


GRAPH = "https://graph.microsoft.com/v1.0"
CONFIG_FILENAME = "state-sync.json"
OVERRIDES_FILENAME = "state-sync-overrides.json"
ARCHIVE_MANIFEST = ".hermes-state-manifest.json"
ARCHIVE_NAME = "hermes-owner-state-v1.tar.gz"
POINTER_NAME = "hermes-state-current-v2.json"
SNAPSHOT_PREFIX = "hermes-state-snapshot-"
STATE_SCHEMA = 1
COMPONENT_SCHEMAS = {
    "memory": 1,
    "skills": 1,
    "config_overrides": 1,
    "pending_approvals": 1,
}
WRITER_PROTOCOL = os.environ.get("STATE_WRITER_PROTOCOL", "dual-v1-v2")
RELEASE_ID = os.environ.get("HERMES_RELEASE_ID", "development")
MAX_POINTER_BYTES = 128_000
MAX_SNAPSHOT_HISTORY = 5
DEFAULT_MAX_ARCHIVE_BYTES = 240_000_000
HARD_MAX_ARCHIVE_BYTES = 250_000_000
MAX_CONFIG_PATHS = 64
MAX_PATH_LENGTH = 240

DEFAULT_CONFIG = {
    "version": 1,
    "enabled": True,
    "include_paths": [
        "memories",
        "skills",
        "SOUL.md",
        "skill-bundles",
        "pending/memory",
        "pending/skills",
    ],
    "exclude_paths": ["skills/.curator_backups"],
    "max_archive_bytes": DEFAULT_MAX_ARCHIVE_BYTES,
    "post_turn_settle_seconds": 3,
}

# These are denied even if a future or model-edited whitelist names them.  State synchronization is
# not a credential backup, a code-deployment mechanism, or a way to collapse caller-private chat
# history into one shared archive.
_HARD_DENIED_ROOTS = frozenset({
    ".git", "audio_cache", "backups", "bin", "bootstrap-cache", "browser_screenshots",
    "cache", "checkpoints", "cron", "hermes-agent", "image_cache", "images", "logs",
    "mcp-tokens", "node", "pastes", "plugins", "runtime", "sandboxes", "sessions",
    "terminal-sessions", "tmp", "venvs",
})
_HARD_DENIED_NAMES = frozenset({
    ".env", "auth.json", "config.yaml", "gateway.pid", "gateway.sock", "state.db",
    "kanban.db", "projects.db", "verification_evidence.db", "project-manager-credentials.json",
})
_SECRET_NAME = re.compile(
    r"(?i)^(access[_-]?token|refresh[_-]?token|client[_-]?secret|credentials?|secrets?|tokens?)"
    r"(?:\.[a-z0-9_-]+)?$"
)
_RUNTIME_SUFFIXES = (".lock", ".pid", ".sock", ".wal", ".shm")


class StateSyncError(RuntimeError):
    """Safe-to-log state synchronization failure without token or document content."""


class StateSyncConflict(StateSyncError):
    """The remote archive changed since this session restored it."""


@dataclass(frozen=True)
class RemoteArchive:
    etag: str
    size: int
    download_url: str


@dataclass(frozen=True)
class ArchiveContents:
    """Validated archive data; paths are normalized and selected by the embedded config."""

    config: dict
    files: dict[str, tuple[bytes, int]]
    hashes: dict[str, str]


@dataclass(frozen=True)
class RemoteStateHead:
    """Validated remote state selected from the legacy bridge or the v2 pointer."""

    content: bytes
    digest: str
    legacy_etag: str
    pointer_etag: str
    pointer: dict | None
    needs_pointer: bool


def _normalise_path(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("state-sync paths must be strings")
    value = value.strip().replace("\\", "/").strip("/")
    pure = pathlib.PurePosixPath(value)
    if (not value or value == "." or pure.is_absolute() or ".." in pure.parts
            or len(value) > MAX_PATH_LENGTH):
        raise ValueError(f"unsafe state-sync path: {value!r}")
    return pure.as_posix()


def validate_config(value: object) -> dict:
    """Return a bounded, normalized config suitable for archive and restore decisions."""
    if not isinstance(value, dict):
        raise ValueError("state-sync config must be a JSON object")
    includes = value.get("include_paths", DEFAULT_CONFIG["include_paths"])
    excludes = value.get("exclude_paths", DEFAULT_CONFIG["exclude_paths"])
    if not isinstance(includes, list) or not isinstance(excludes, list):
        raise ValueError("include_paths and exclude_paths must be arrays")
    if not includes or len(includes) > MAX_CONFIG_PATHS or len(excludes) > MAX_CONFIG_PATHS:
        raise ValueError("state-sync path list is empty or too large")

    include_paths = list(dict.fromkeys(_normalise_path(item) for item in includes))
    exclude_paths = list(dict.fromkeys(_normalise_path(item) for item in excludes))
    max_bytes = int(value.get("max_archive_bytes", DEFAULT_MAX_ARCHIVE_BYTES))
    if max_bytes < 1_000_000 or max_bytes > HARD_MAX_ARCHIVE_BYTES:
        raise ValueError(
            f"max_archive_bytes must be between 1000000 and {HARD_MAX_ARCHIVE_BYTES}"
        )
    settle = int(value.get(
        "post_turn_settle_seconds", DEFAULT_CONFIG["post_turn_settle_seconds"]))
    if settle < 0 or settle > 15:
        raise ValueError("post_turn_settle_seconds must be between 0 and 15")
    return {
        "version": 1,
        "enabled": bool(value.get("enabled", True)),
        "include_paths": include_paths,
        "exclude_paths": exclude_paths,
        "max_archive_bytes": max_bytes,
        "post_turn_settle_seconds": settle,
    }


def load_config(home: pathlib.Path) -> dict:
    defaults = _load_defaults()
    overrides_path = home / OVERRIDES_FILENAME
    if overrides_path.exists():
        try:
            overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
            return _apply_overrides(defaults, overrides)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StateSyncError(f"invalid {OVERRIDES_FILENAME}: {type(exc).__name__}") from None

    # Compatibility with the archive written by release 80. It contains a fully materialized
    # state-sync.json rather than an override document. Preserve its exact effective behavior until
    # ensure_config_migrated() converts it relative to the defaults in the current image.
    path = home / CONFIG_FILENAME
    if not path.exists():
        return defaults
    try:
        return validate_config(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise StateSyncError(f"invalid {CONFIG_FILENAME}: {type(exc).__name__}") from None


def _load_defaults() -> dict:
    path = pathlib.Path(os.environ.get(
        "STATE_SYNC_DEFAULTS_PATH", "/opt/state-sync.defaults.json"))
    if path.exists():
        try:
            return validate_config(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StateSyncError(f"invalid packaged state-sync defaults: {type(exc).__name__}") from None
    return validate_config(DEFAULT_CONFIG)


def _apply_overrides(defaults: dict, value: object) -> dict:
    if not isinstance(value, dict) or int(value.get("schema", 0)) != 1:
        raise ValueError("state-sync overrides must use schema 1")
    allowed = {
        "schema", "add_include_paths", "remove_include_paths", "add_exclude_paths",
        "remove_exclude_paths", "enabled", "max_archive_bytes", "post_turn_settle_seconds",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError("state-sync overrides contain unknown fields")

    def paths(name: str) -> list[str]:
        raw = value.get(name, [])
        if not isinstance(raw, list) or len(raw) > MAX_CONFIG_PATHS:
            raise ValueError(f"{name} must be a bounded array")
        return list(dict.fromkeys(_normalise_path(item) for item in raw))

    includes = [p for p in defaults["include_paths"] if p not in set(paths("remove_include_paths"))]
    for path in paths("add_include_paths"):
        if path not in includes:
            includes.append(path)
    excludes = [p for p in defaults["exclude_paths"] if p not in set(paths("remove_exclude_paths"))]
    for path in paths("add_exclude_paths"):
        if path not in excludes:
            excludes.append(path)
    merged = {
        **defaults,
        "include_paths": includes,
        "exclude_paths": excludes,
    }
    for name in ("enabled", "max_archive_bytes", "post_turn_settle_seconds"):
        if name in value:
            merged[name] = value[name]
    return validate_config(merged)


def _config_to_overrides(config: dict, defaults: dict) -> dict:
    value: dict[str, object] = {
        "schema": 1,
        "add_include_paths": [p for p in config["include_paths"] if p not in defaults["include_paths"]],
        "remove_include_paths": [p for p in defaults["include_paths"] if p not in config["include_paths"]],
        "add_exclude_paths": [p for p in config["exclude_paths"] if p not in defaults["exclude_paths"]],
        "remove_exclude_paths": [p for p in defaults["exclude_paths"] if p not in config["exclude_paths"]],
    }
    for name in ("enabled", "max_archive_bytes", "post_turn_settle_seconds"):
        if config[name] != defaults[name]:
            value[name] = config[name]
    return value


def _write_json_atomic(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    try:
        if path.read_bytes() == encoded:
            return
    except FileNotFoundError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def write_config(home: pathlib.Path, value: object) -> dict:
    """Write owner overrides plus a release-80-compatible materialized configuration."""
    config = validate_config(value)
    defaults = _load_defaults()
    _write_json_atomic(home / OVERRIDES_FILENAME, _config_to_overrides(config, defaults))
    # The bridge dual-writes the old archive while release-80 sessions may still exist. Keep the
    # effective legacy file materialized so those sessions read the same whitelist.
    _write_json_atomic(home / CONFIG_FILENAME, config)
    return config


def ensure_config_migrated(home: pathlib.Path) -> dict:
    """Convert a legacy complete config into defaults + overrides without changing behavior."""
    config = load_config(home)
    if not (home / OVERRIDES_FILENAME).exists():
        return write_config(home, config)
    # Re-materialize state-sync.json when a newer image changes defaults but the owner has only
    # sparse overrides. This is solely for compatibility with old dual-write sessions.
    _write_json_atomic(home / CONFIG_FILENAME, config)
    return config


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def _hard_denied(relative: str) -> bool:
    parts = pathlib.PurePosixPath(relative).parts
    if not parts:
        return True
    if parts[0] in _HARD_DENIED_ROOTS:
        return True
    name = parts[-1]
    if name in _HARD_DENIED_NAMES or name.startswith(".env."):
        return True
    if name.endswith(_RUNTIME_SUFFIXES) or _SECRET_NAME.search(name):
        return True
    return False


def path_is_selected(relative: str, config: dict) -> bool:
    """Apply editable roots, editable exclusions, then non-overridable security exclusions."""
    try:
        relative = _normalise_path(relative)
    except ValueError:
        return False
    if relative in {CONFIG_FILENAME, OVERRIDES_FILENAME}:
        return True
    if _hard_denied(relative):
        return False
    if not any(_under(relative, root) for root in config["include_paths"]):
        return False
    return not any(_under(relative, root) for root in config["exclude_paths"])


def _conflict_path(relative: str, digest: str) -> str:
    root = "pending/skills" if relative.startswith("skills/") else "pending/memory"
    prefix = f"{root}/state-sync-conflicts/{digest[:12]}"
    candidate = f"{prefix}/{relative}"
    if len(candidate) <= MAX_PATH_LENGTH:
        return candidate
    # Preserve a recognizable basename while bounding a deeply nested original path. The full
    # original path remains represented by a digest, so distinct long paths do not collapse.
    name = pathlib.PurePosixPath(relative).name[-96:]
    path_digest = hashlib.sha256(relative.encode()).hexdigest()[:12]
    return f"{prefix}/{path_digest}-{name}"


def selected_files(home: pathlib.Path, config: dict) -> list[tuple[str, pathlib.Path]]:
    """Enumerate regular selected files without following symlinks outside the state root."""
    result: list[tuple[str, pathlib.Path]] = []
    candidates = [CONFIG_FILENAME, OVERRIDES_FILENAME, *config["include_paths"]]
    seen: set[str] = set()
    home_real = home.resolve()
    for entry in candidates:
        path = home / entry
        if path.is_symlink():
            continue
        if path.is_file():
            paths = [path]
        elif path.is_dir():
            paths = (candidate for candidate in path.rglob("*") if candidate.is_file())
        else:
            continue
        for candidate in paths:
            if candidate.is_symlink():
                continue
            try:
                resolved = candidate.resolve()
                relative = resolved.relative_to(home_real).as_posix()
            except (OSError, ValueError):
                continue
            if relative in seen or not path_is_selected(relative, config):
                continue
            seen.add(relative)
            result.append((relative, resolved))
    return sorted(result)


def unlisted_durable_candidates(home: pathlib.Path, config: dict) -> list[str]:
    """Return safe top-level paths not currently selected, without reading their contents.

    This makes whitelist evolution visible instead of silently losing every new Hermes state root.
    Hard-denied runtime/secret paths are intentionally absent: they can never be opted in.
    """
    result = []
    try:
        entries = list(home.iterdir())
    except OSError:
        return result
    for entry in entries:
        name = entry.name
        if entry.is_symlink() or _hard_denied(name):
            continue
        try:
            normalized = _normalise_path(name)
        except ValueError:
            continue
        if any(_under(normalized, root) or _under(root, normalized)
               for root in config["include_paths"]):
            continue
        if normalized in {CONFIG_FILENAME, OVERRIDES_FILENAME}:
            continue
        result.append(normalized)
    return sorted(result)[:MAX_CONFIG_PATHS]


def file_fingerprint(home: pathlib.Path, config: dict) -> str:
    """Cheap change detector; archive content hashes still provide the authoritative manifest."""
    digest = hashlib.sha256()
    for relative, path in selected_files(home, config):
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(relative.encode())
        digest.update(f"\0{stat.st_size}\0{stat.st_mtime_ns}\0".encode())
    return digest.hexdigest()


def config_fingerprint(config: dict) -> str:
    return hashlib.sha256(json.dumps(
        config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_archive(home: pathlib.Path, config: dict) -> tuple[bytes, str, int]:
    """Build a deterministic gzip tarball and return bytes, SHA-256 and selected file count."""
    files = selected_files(home, config)
    manifest_files: dict[str, dict[str, object]] = {}
    payloads: list[tuple[str, pathlib.Path, bytes, os.stat_result]] = []
    total_uncompressed = 0
    for relative, path in files:
        try:
            data = path.read_bytes()
            stat = path.stat()
        except OSError:
            continue
        total_uncompressed += len(data)
        if total_uncompressed > config["max_archive_bytes"]:
            raise StateSyncError("selected state exceeds max_archive_bytes before compression")
        manifest_files[relative] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
        payloads.append((relative, path, data, stat))

    manifest = json.dumps({
        "format": 1,
        "files": manifest_files,
    }, sort_keys=True, separators=(",", ":")).encode()

    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
            manifest_info = tarfile.TarInfo(ARCHIVE_MANIFEST)
            manifest_info.size = len(manifest)
            manifest_info.mode = 0o600
            manifest_info.mtime = 0
            archive.addfile(manifest_info, io.BytesIO(manifest))
            for relative, _path, data, stat in payloads:
                info = tarfile.TarInfo(relative)
                info.size = len(data)
                info.mode = stat.st_mode & 0o777
                # Content and mode define durable state. Normalizing mtime keeps an unchanged
                # restore byte-identical to its remote archive instead of uploading it again merely
                # because extraction created a new local timestamp.
                info.mtime = 0
                archive.addfile(info, io.BytesIO(data))
    content = output.getvalue()
    if len(content) > config["max_archive_bytes"]:
        raise StateSyncError("compressed state exceeds max_archive_bytes")
    return content, hashlib.sha256(content).hexdigest(), len(payloads)


def _config_from_archive(archive: tarfile.TarFile, fallback: dict) -> dict:
    try:
        member = archive.getmember(CONFIG_FILENAME)
    except KeyError:
        return fallback
    if not member.isfile() or member.size > 128_000:
        raise StateSyncError("archive contains an invalid state-sync config")
    stream = archive.extractfile(member)
    if stream is None:
        raise StateSyncError("archive state-sync config could not be read")
    try:
        return validate_config(json.loads(stream.read().decode("utf-8")))
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
        raise StateSyncError("archive contains an invalid state-sync config") from None


def _read_archive(content: bytes, local_config: dict) -> ArchiveContents:
    """Fully validate an archive before any member is allowed to touch durable state."""
    if len(content) > HARD_MAX_ARCHIVE_BYTES:
        raise StateSyncError("remote state archive exceeds the hard size limit")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(content), mode="r:gz")
    except (tarfile.TarError, OSError):
        raise StateSyncError("remote state archive is not a valid gzip tarball") from None
    with archive:
        remote_config = _config_from_archive(archive, local_config)
        try:
            manifest_member = archive.getmember(ARCHIVE_MANIFEST)
        except KeyError:
            raise StateSyncError("remote archive has no integrity manifest") from None
        if not manifest_member.isfile() or manifest_member.size > 4_000_000:
            raise StateSyncError("remote archive has an invalid integrity manifest")
        manifest_stream = archive.extractfile(manifest_member)
        if manifest_stream is None:
            raise StateSyncError("remote archive manifest could not be read")
        try:
            manifest = json.loads(manifest_stream.read().decode("utf-8"))
            manifest_files = manifest["files"]
            if manifest.get("format") != 1 or not isinstance(manifest_files, dict):
                raise ValueError
        except (UnicodeDecodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise StateSyncError("remote archive has an invalid integrity manifest") from None

        files: dict[str, tuple[bytes, int]] = {}
        hashes: dict[str, str] = {}
        total = 0
        for member in archive.getmembers():
            if member.name == ARCHIVE_MANIFEST:
                continue
            try:
                relative = _normalise_path(member.name)
            except ValueError:
                raise StateSyncError("remote archive contains an unsafe path") from None
            if relative in files:
                raise StateSyncError("remote archive contains duplicate paths")
            if not member.isfile() or not path_is_selected(relative, remote_config):
                raise StateSyncError("remote archive contains a non-selected or non-file entry")
            total += member.size
            if total > remote_config["max_archive_bytes"]:
                raise StateSyncError("remote archive expands beyond max_archive_bytes")
            source = archive.extractfile(member)
            if source is None:
                raise StateSyncError("remote archive member could not be read")
            data = source.read(member.size + 1)
            if len(data) != member.size:
                raise StateSyncError("remote archive member size does not match its header")
            recorded = manifest_files.get(relative)
            digest = hashlib.sha256(data).hexdigest()
            if (not isinstance(recorded, dict) or recorded.get("size") != len(data)
                    or recorded.get("sha256") != digest):
                raise StateSyncError("remote archive failed its integrity manifest")
            files[relative] = (data, member.mode & 0o777)
            hashes[relative] = digest
        if set(manifest_files) != set(files):
            raise StateSyncError("remote archive manifest and members do not match")
        return ArchiveContents(remote_config, files, hashes)


def _overlay_files(home: pathlib.Path, files: dict[str, tuple[bytes, int]]) -> int:
    """Stage validated files outside HERMES_HOME, then atomically replace them one by one."""
    staging = pathlib.Path(tempfile.mkdtemp(prefix="hermes-state-restore-"))
    try:
        for relative, (data, mode) in files.items():
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            os.chmod(destination, mode)
        restored = 0
        for source in sorted(staging.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(staging)
            destination = home / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            restored += 1
        return restored
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _restore_archive(home: pathlib.Path, content: bytes, local_config: dict) -> tuple[int, dict[str, str]]:
    parsed = _read_archive(content, local_config)
    return _overlay_files(home, parsed.files), parsed.hashes


def restore_archive(home: pathlib.Path, content: bytes, local_config: dict) -> int:
    """Validate and atomically overlay selected archive files onto ``home``."""
    restored, _hashes = _restore_archive(home, content, local_config)
    return restored


class StateSyncManager:
    """Synchronize whitelisted state using a turn-scoped Agent User Graph token."""

    def __init__(self, home: str | os.PathLike[str], logger=print):
        self.home = pathlib.Path(home)
        self.log = logger
        self._lock = threading.Lock()
        self._prepared = False
        self._legacy_etag = ""
        self._pointer_etag = ""
        self._pointer: dict | None = None
        self._needs_pointer = False
        self._write_blocked = True
        self._uploaded_hash = ""
        self._fingerprint = ""
        self._config_fingerprint = ""
        self._base_hashes: dict[str, str] = {}

    @staticmethod
    def _request(url: str, token: str, *, method="GET", data=None, headers=None, timeout=60):
        request_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            **(headers or {}),
        }
        request = urllib.request.Request(
            url, data=data, method=method, headers=request_headers)
        return urllib.request.urlopen(request, timeout=timeout)

    def _metadata_name(self, token: str, item_name: str) -> RemoteArchive | None:
        name = urllib.parse.quote(item_name, safe="")
        url = (f"{GRAPH}/me/drive/special/approot:/{name}"
               "?$select=id,eTag,size,@microsoft.graph.downloadUrl")
        try:
            with self._request(url, token) as response:
                value = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                error.read(4096)
            finally:
                error.close()
            if error.code == 404:
                return None
            raise StateSyncError(f"Graph metadata request failed with HTTP {error.code}") from None
        return RemoteArchive(
            etag=str(value.get("eTag") or ""),
            size=int(value.get("size") or 0),
            download_url=str(value.get("@microsoft.graph.downloadUrl") or ""),
        )

    # Kept as a narrow compatibility seam for tests and the release-80 merge path.
    def _metadata(self, token: str) -> RemoteArchive | None:
        return self._metadata_name(token, ARCHIVE_NAME)

    @staticmethod
    def _trusted_download_url(url: str) -> bool:
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and (
            host.endswith(".sharepoint.com") or host.endswith(".sharepoint-df.com")
            or host.endswith(".1drv.com") or host.endswith(".onedrive.com")
        )

    def _download(self, remote: RemoteArchive, max_bytes: int) -> bytes:
        if remote.size > max_bytes or not self._trusted_download_url(remote.download_url):
            raise StateSyncError("remote state archive is too large or has an untrusted download URL")
        request = urllib.request.Request(remote.download_url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                content = response.read(max_bytes + 1)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise StateSyncError(f"state archive download failed with HTTP {code}") from None
        if len(content) > max_bytes:
            raise StateSyncError("remote state archive exceeded max_archive_bytes while downloading")
        return content

    @staticmethod
    def _validate_pointer(value: object) -> dict:
        """Validate the untrusted mutable pointer before following its snapshot name."""
        if not isinstance(value, dict) or value.get("format") != 2:
            raise StateSyncError("remote state pointer has an unsupported format")
        if value.get("state_schema") != STATE_SCHEMA:
            raise StateSyncError("remote state pointer needs an unsupported state migration")
        schemas = value.get("component_schemas")
        if not isinstance(schemas, dict) or schemas != COMPONENT_SCHEMAS:
            raise StateSyncError("remote state pointer needs an unsupported component migration")
        snapshot = value.get("snapshot")
        if not isinstance(snapshot, dict):
            raise StateSyncError("remote state pointer has no snapshot")
        name = snapshot.get("name")
        digest = snapshot.get("sha256")
        size = snapshot.get("size")
        expected_name = f"{SNAPSHOT_PREFIX}{digest}.tar.gz"
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or name != expected_name or not isinstance(size, int)
                or size < 0 or size > HARD_MAX_ARCHIVE_BYTES):
            raise StateSyncError("remote state pointer contains an invalid snapshot reference")
        history = value.get("history", [])
        if not isinstance(history, list) or len(history) > MAX_SNAPSHOT_HISTORY:
            raise StateSyncError("remote state pointer contains invalid history")
        for entry in history:
            if not isinstance(entry, dict):
                raise StateSyncError("remote state pointer contains invalid history")
            historical_digest = entry.get("sha256")
            if (not isinstance(historical_digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", historical_digest)
                    or entry.get("name") != f"{SNAPSHOT_PREFIX}{historical_digest}.tar.gz"
                    or not isinstance(entry.get("size"), int)
                    or entry["size"] < 0 or entry["size"] > HARD_MAX_ARCHIVE_BYTES):
                raise StateSyncError("remote state pointer contains invalid history")
        legacy_etag = value.get("legacy_etag", "")
        if not isinstance(legacy_etag, str):
            raise StateSyncError("remote state pointer contains an invalid legacy bridge ETag")
        writer = value.get("writer")
        if (not isinstance(writer, dict) or not isinstance(writer.get("release"), str)
                or not isinstance(writer.get("protocol"), str)
                or not 0 < len(writer["release"]) <= 256
                or not 0 < len(writer["protocol"]) <= 64):
            raise StateSyncError("remote state pointer contains invalid writer metadata")
        return value

    def _read_pointer_document(
        self, token: str, remote: RemoteArchive,
    ) -> dict:
        if remote.size > MAX_POINTER_BYTES:
            raise StateSyncError("remote state pointer exceeds its size limit")
        content = self._download(remote, MAX_POINTER_BYTES)
        try:
            value = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise StateSyncError("remote state pointer is not valid JSON") from None
        return self._validate_pointer(value)

    def _remote_head(self, token: str, config: dict) -> RemoteStateHead | None:
        """Resolve the newest compatible head across the v1 bridge and v2 pointer.

        Release 80 knows only the mutable v1 archive. During the bridge, every new writer updates
        that archive too. If its ETag differs from the one recorded by the v2 pointer, a legacy
        writer won after the pointer was published, so the legacy bytes are authoritative and the
        next finish pass advances v2. This permits old and new Foundry sessions to coexist.
        """
        pointer_remote = self._metadata_name(token, POINTER_NAME)
        legacy = self._metadata(token)

        # Normal turns pay only two small metadata requests. Download the pointer/archive only when
        # an ETag changed or this process has not hydrated yet.
        if (self._prepared and legacy is not None and legacy.etag == self._legacy_etag
                and ((pointer_remote is None and not self._pointer_etag)
                     or (pointer_remote is not None and pointer_remote.etag == self._pointer_etag))):
            return RemoteStateHead(
                content=b"",
                digest=self._uploaded_hash,
                legacy_etag=legacy.etag,
                pointer_etag=pointer_remote.etag if pointer_remote else "",
                pointer=self._pointer,
                needs_pointer=pointer_remote is None or self._needs_pointer,
            )

        if pointer_remote is not None:
            pointer = self._read_pointer_document(token, pointer_remote)
            if legacy is None:
                raise StateSyncError("v2 state exists but its legacy compatibility head is missing")
            if legacy.etag == pointer.get("legacy_etag"):
                snapshot = pointer["snapshot"]
                snapshot_remote = self._metadata_name(token, snapshot["name"])
                if snapshot_remote is None:
                    raise StateSyncError("remote state pointer references a missing snapshot")
                if snapshot_remote.size != snapshot["size"]:
                    raise StateSyncError("remote state snapshot size does not match its pointer")
                pointer_content = self._download(
                    snapshot_remote, config["max_archive_bytes"])
                if hashlib.sha256(pointer_content).hexdigest() != snapshot["sha256"]:
                    raise StateSyncError("remote state snapshot hash does not match its pointer")
                _read_archive(pointer_content, config)
                return RemoteStateHead(
                    content=pointer_content,
                    digest=pointer["snapshot"]["sha256"],
                    legacy_etag=legacy.etag,
                    pointer_etag=pointer_remote.etag,
                    pointer=pointer,
                    needs_pointer=False,
                )
            legacy_content = self._download(legacy, config["max_archive_bytes"])
            _read_archive(legacy_content, config)
            return RemoteStateHead(
                content=legacy_content,
                digest=hashlib.sha256(legacy_content).hexdigest(),
                legacy_etag=legacy.etag,
                pointer_etag=pointer_remote.etag,
                pointer=pointer,
                needs_pointer=True,
            )
        if legacy is None:
            return None
        content = self._download(legacy, config["max_archive_bytes"])
        _read_archive(content, config)
        return RemoteStateHead(
            content=content,
            digest=hashlib.sha256(content).hexdigest(),
            legacy_etag=legacy.etag,
            pointer_etag="",
            pointer=None,
            needs_pointer=True,
        )

    def prepare_turn(self, token: str) -> None:
        """Refresh durable state before every verified-owner Hermes turn.

        The first turn restores the complete selected archive. Later turns compare the remote head
        and merge it when another Foundry session (for example email versus Teams) advanced state.
        Local unsynchronized changes are never blindly replaced.
        """
        if not token:
            return
        with self._lock:
            # A failed refresh must never be followed by an upload from an unknown/stale base. Only
            # a completely successful head resolution and migration unblocks writes for this turn.
            self._write_blocked = True
            config = load_config(self.home)
            head = self._remote_head(token, config)
            if head is None:
                self._base_hashes = {}
                self._legacy_etag = ""
                self._pointer_etag = ""
                self._pointer = None
                self._needs_pointer = True
                self._uploaded_hash = ""
                if not self._prepared:
                    self.log("[state-sync] no remote owner-state snapshot")
            elif not self._prepared:
                count, self._base_hashes = _restore_archive(self.home, head.content, config)
                self.log(
                    f"[state-sync] restored owner state files={count} bytes={len(head.content)}")
            elif head.digest != self._uploaded_hash or head.legacy_etag != self._legacy_etag:
                remote_archive = _read_archive(head.content, config)
                additions, deletions, collisions = self._merge_remote_into_local(remote_archive)
                self.log(
                    f"[state-sync] refreshed concurrent owner state files={additions} "
                    f"deletions={deletions} conflicts={collisions}"
                )
                self._base_hashes = remote_archive.hashes

            if head is not None:
                self._legacy_etag = head.legacy_etag
                self._pointer_etag = head.pointer_etag
                self._pointer = head.pointer
                self._needs_pointer = head.needs_pointer
                self._uploaded_hash = head.digest

            # Convert release-80's complete config into sparse owner overrides only after a fully
            # validated restore. Keep materializing the old file while legacy sessions can run.
            current = ensure_config_migrated(self.home)
            self._fingerprint = file_fingerprint(self.home, current)
            self._config_fingerprint = config_fingerprint(current)
            self._prepared = True
            self._write_blocked = False

    def _upload_name(
        self, token: str, item_name: str, content: bytes, etag: str | None,
        content_type: str,
    ) -> str:
        name = urllib.parse.quote(item_name, safe="")
        url = f"{GRAPH}/me/drive/special/approot:/{name}:/content"
        headers = {"Content-Type": content_type}
        headers["If-Match" if etag else "If-None-Match"] = etag or "*"
        try:
            with self._request(
                url, token, method="PUT", data=content, headers=headers, timeout=120
            ) as response:
                value = json.load(response)
        except urllib.error.HTTPError as error:
            code = error.code
            try:
                error.read(4096)
            finally:
                error.close()
            if code in (409, 412):
                raise StateSyncConflict("remote state changed concurrently") from None
            raise StateSyncError(f"Graph state upload failed with HTTP {code}") from None
        return str(value.get("eTag") or "")

    def _upload(self, token: str, content: bytes, etag: str | None) -> str:
        return self._upload_name(
            token, ARCHIVE_NAME, content, etag, "application/gzip")

    def finish_turn(self, token: str) -> None:
        """Upload changed state before turn authority is erased.

        A short second pass catches quick post-response background learning. A longer-running
        background review is picked up at the end of the owner's next turn; no token survives this
        method.
        """
        if not token:
            return
        with self._lock:
            if self._write_blocked:
                self.log("[state-sync] save skipped because remote refresh did not complete")
                return
            config = load_config(self.home)
            if not config["enabled"]:
                # A transition to disabled gets one final durable checkpoint so the choice survives
                # a new Foundry session. Subsequent disabled turns do not upload memory/skill work.
                if config_fingerprint(config) != self._config_fingerprint:
                    self._sync_once(token, config)
                    self._config_fingerprint = config_fingerprint(config)
                return
            fingerprint = file_fingerprint(self.home, config)
            if fingerprint != self._fingerprint or self._needs_pointer:
                self._sync_once(token, config)
            settle = config["post_turn_settle_seconds"]
            if settle:
                time.sleep(settle)
                refreshed = load_config(self.home)
                if refreshed["enabled"]:
                    fingerprint = file_fingerprint(self.home, refreshed)
                    if fingerprint != self._fingerprint:
                        self._sync_once(token, refreshed)

    def _sync_once(self, token: str, config: dict) -> None:
        config = ensure_config_migrated(self.home)
        content, digest, count = build_archive(self.home, config)
        self._fingerprint = file_fingerprint(self.home, config)
        if digest == self._uploaded_hash and not self._needs_pointer:
            return
        if digest != self._uploaded_hash:
            try:
                new_etag = self._upload(token, content, self._legacy_etag or None)
            except StateSyncConflict:
                resolved = self._merge_and_retry(token, content, config)
                if resolved is None:
                    self.log("[state-sync] owner-state conflict; both snapshots retained locally")
                    return
                new_etag, content, digest, count = resolved
            self._legacy_etag = new_etag

        # The compatibility head is already durable at this point. Record that fact even if the
        # secondary v2 pointer publication is interrupted, so a second settle pass does not create
        # an unnecessary new ETag for identical bytes.
        self._uploaded_hash = digest
        self._base_hashes = _read_archive(content, config).hashes
        self._config_fingerprint = config_fingerprint(config)

        # Publish immutable content before atomically advancing the v2 pointer. The mutable v1
        # archive remains current throughout this bridge, so release-80 sessions stay compatible.
        if not self._publish_pointer(token, content, digest):
            self._needs_pointer = True
            self.log("[state-sync] state saved through legacy bridge; v2 pointer update deferred")
            return
        self._needs_pointer = False
        self.log(f"[state-sync] uploaded owner state files={count} bytes={len(content)}")

    def _merge_and_retry(
        self, token: str, local_content: bytes, config: dict,
    ) -> tuple[str, bytes, str, int] | None:
        """Merge a concurrently updated snapshot without silently discarding either side.

        Files changed only remotely are applied to the local tree. Files changed independently on
        both sides keep the local version at its normal path and preserve the remote version under a
        selected pending conflict path for explicit reconciliation. This is deliberately a content
        merge, not last-writer-wins.
        """
        remote = self._metadata(token)
        if remote is None:
            return None
        remote_content = self._download(remote, config["max_archive_bytes"])
        remote_archive = _read_archive(remote_content, config)
        additions, deletions, collisions = self._merge_remote_into_local(remote_archive)
        self._base_hashes = remote_archive.hashes
        merged_config = load_config(self.home)
        merged_content, merged_digest, merged_count = build_archive(self.home, merged_config)
        try:
            new_etag = self._upload(token, merged_content, remote.etag or None)
        except StateSyncConflict:
            # A third writer won this bounded attempt. Leave the local tree, including preserved
            # conflict copies, untouched; a later owner turn can retry without data loss.
            return None
        self.log(
            f"[state-sync] merged concurrent owner state remote_files={additions} "
            f"deletions={deletions} conflicts={collisions}"
        )
        return new_etag, merged_content, merged_digest, merged_count

    def _merge_remote_into_local(
        self, remote_archive: ArchiveContents,
    ) -> tuple[int, int, int]:
        """Three-way merge a newer remote head into this session before an owner turn.

        The last head seen by this process is the base. Remote-only changes and deletions are
        applied. Local-only changes remain pending for the next save. A true same-path conflict
        keeps the local working copy and preserves the remote bytes under an approved pending path.
        """
        config = load_config(self.home)
        local_content, _digest, _count = build_archive(self.home, config)
        local_archive = _read_archive(local_content, config)
        additions: dict[str, tuple[bytes, int]] = {}
        deletions: list[str] = []
        collisions = 0
        all_paths = set(self._base_hashes) | set(local_archive.hashes) | set(remote_archive.hashes)
        for relative in sorted(all_paths):
            base_hash = self._base_hashes.get(relative)
            local_hash = local_archive.hashes.get(relative)
            remote_hash = remote_archive.hashes.get(relative)
            if local_hash == remote_hash:
                continue
            if local_hash == base_hash:
                if remote_hash is None:
                    # Absence means deletion only when the remote writer's own effective policy
                    # selected that path. An older release may simply not know a newly added
                    # default root; treating that omission as a deletion would make the
                    # expand/contract bridge destructive.
                    if path_is_selected(relative, remote_archive.config):
                        deletions.append(relative)
                else:
                    additions[relative] = remote_archive.files[relative]
                continue
            if remote_hash == base_hash:
                continue

            # Both heads changed. The active local version remains authoritative for this turn.
            # Preserve remote content when it exists; a remote deletion needs no extra byte copy.
            if remote_hash is not None:
                conflict_path = _conflict_path(relative, remote_hash)
                additions[conflict_path] = remote_archive.files[relative]
            collisions += 1

        for relative in deletions:
            try:
                destination = (self.home / relative).resolve()
                destination.relative_to(self.home.resolve())
                if destination.is_file() and not destination.is_symlink():
                    destination.unlink()
            except (OSError, ValueError):
                raise StateSyncError("could not apply a remote state deletion") from None
        if additions:
            _overlay_files(self.home, additions)
        return len(additions), len(deletions), collisions

    @staticmethod
    def _snapshot_entry(content: bytes, digest: str) -> dict[str, object]:
        return {
            "name": f"{SNAPSHOT_PREFIX}{digest}.tar.gz",
            "sha256": digest,
            "size": len(content),
        }

    def _ensure_snapshot(self, token: str, content: bytes, digest: str) -> dict:
        snapshot = self._snapshot_entry(content, digest)
        try:
            self._upload_name(
                token, str(snapshot["name"]), content, None, "application/gzip")
            return snapshot
        except StateSyncConflict:
            # Content-addressed names make a pre-existing object safe only after verification.
            remote = self._metadata_name(token, str(snapshot["name"]))
            if remote is None or remote.size != len(content):
                raise StateSyncError("existing immutable state snapshot has an invalid size") from None
            existing = self._download(remote, HARD_MAX_ARCHIVE_BYTES)
            if hashlib.sha256(existing).hexdigest() != digest:
                raise StateSyncError("existing immutable state snapshot failed hash verification")
            return snapshot

    @staticmethod
    def _pointer_history(current: dict | None, latest: dict | None = None) -> list[dict]:
        entries: list[dict] = []
        for pointer in (latest, current):
            if not pointer:
                continue
            snapshot = pointer.get("snapshot")
            if isinstance(snapshot, dict):
                entries.append(snapshot)
            history = pointer.get("history", [])
            if isinstance(history, list):
                entries.extend(item for item in history if isinstance(item, dict))
        result: list[dict] = []
        seen: set[str] = set()
        for entry in entries:
            digest = entry.get("sha256")
            if isinstance(digest, str) and digest not in seen:
                seen.add(digest)
                result.append({
                    "name": entry.get("name"),
                    "sha256": digest,
                    "size": entry.get("size"),
                })
            if len(result) >= MAX_SNAPSHOT_HISTORY:
                break
        return result

    def _pointer_document(
        self, snapshot: dict, *, latest: dict | None = None,
    ) -> dict:
        history = [
            item for item in self._pointer_history(self._pointer, latest)
            if item.get("sha256") != snapshot.get("sha256")
        ][:MAX_SNAPSHOT_HISTORY]
        return {
            "format": 2,
            "state_schema": STATE_SCHEMA,
            "component_schemas": dict(COMPONENT_SCHEMAS),
            "snapshot": snapshot,
            "history": history,
            "legacy_etag": self._legacy_etag,
            "writer": {
                "release": RELEASE_ID,
                "protocol": WRITER_PROTOCOL,
            },
        }

    def _publish_pointer(self, token: str, content: bytes, digest: str) -> bool:
        snapshot = self._ensure_snapshot(token, content, digest)
        pointer = self._pointer_document(snapshot)
        encoded = json.dumps(pointer, sort_keys=True, separators=(",", ":")).encode()
        try:
            etag = self._upload_name(
                token, POINTER_NAME, encoded, self._pointer_etag or None, "application/json")
        except StateSyncConflict:
            latest_remote = self._metadata_name(token, POINTER_NAME)
            if latest_remote is None:
                return False
            latest = self._read_pointer_document(token, latest_remote)
            # If a different session advanced the mutable compatibility head after our write, its
            # pointer (or the next bridge pass) must win. Never point backwards merely to satisfy
            # this session's bookkeeping.
            legacy = self._metadata(token)
            if legacy is None or legacy.etag != self._legacy_etag:
                self._pointer_etag = latest_remote.etag
                self._pointer = latest
                return False
            pointer = self._pointer_document(snapshot, latest=latest)
            encoded = json.dumps(pointer, sort_keys=True, separators=(",", ":")).encode()
            try:
                etag = self._upload_name(
                    token, POINTER_NAME, encoded, latest_remote.etag, "application/json")
            except StateSyncConflict:
                return False
        self._pointer_etag = etag
        self._pointer = pointer
        return True

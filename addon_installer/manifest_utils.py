import json
from pathlib import Path

from .constants import UUID_RE
from .path_utils import clean_name


def validate_uuid(uuid, context=""):
    """Validate standard UUID format (8-4-4-4-12 hex)."""
    label = f" in {context}" if context else ""
    if not uuid or not UUID_RE.match(uuid):
        raise RuntimeError(f"Invalid UUID{label}: {uuid!r}")


def load_json(path):
    # utf-8-sig handles BOM that sometimes appears in Windows files
    return json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))


def version_array(version):
    if isinstance(version, list):
        result = version
    elif isinstance(version, str):
        result = [int(x) for x in version.split(".")]
    else:
        raise RuntimeError(f"Unknown version format: {version}")
    if not all(isinstance(x, int) for x in result):
        raise RuntimeError(f"Version must be numeric: {version}")
    # Pad or truncate to exactly 3 elements to tolerate non-standard manifests
    result = (result + [0, 0, 0])[:3]
    return result


def detect_pack_kinds(manifest):
    types = [m.get("type") for m in manifest.get("modules", [])]
    kinds = []
    if "resources" in types:
        kinds.append("rp")
    if "data" in types or "script" in types:
        kinds.append("bp")
    return kinds


def read_lang_entries(path: Path) -> dict:
    """Read Bedrock .lang key/value entries."""
    entries = {}
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return entries
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            entries[key] = value
    return entries


def language_files_for_pack(pack_dir: Path) -> list[Path]:
    """Return likely language files in preference order."""
    texts_dir = pack_dir / "texts"
    if not texts_dir.exists():
        return []

    files = []
    for language in ("en_US", "en_GB"):
        path = texts_dir / f"{language}.lang"
        if path.exists():
            files.append(path)

    languages_json = texts_dir / "languages.json"
    if languages_json.exists():
        try:
            languages = json.loads(languages_json.read_text(encoding="utf-8-sig", errors="replace"))
        except json.JSONDecodeError:
            languages = []
        if isinstance(languages, list):
            for language in languages:
                if not isinstance(language, str):
                    continue
                path = texts_dir / f"{language}.lang"
                if path.exists() and path not in files:
                    files.append(path)

    for path in sorted(texts_dir.glob("*.lang"), key=lambda p: p.name.lower()):
        if path not in files:
            files.append(path)
    return files


def resolve_pack_text(pack_dir: Path, text_key: str):
    """Resolve manifest text keys like pack.name from texts/*.lang."""
    if not text_key:
        return None
    for path in language_files_for_pack(pack_dir):
        value = read_lang_entries(path).get(text_key)
        if value and value != text_key:
            return value
    return None


def manifest_display_name(pack_dir: Path, manifest: dict) -> str:
    """Return localized pack name when manifest header.name is a text key."""
    raw_name = manifest.get("header", {}).get("name") or pack_dir.name
    return resolve_pack_text(pack_dir, raw_name) or raw_name


def pack_folder_name(pack_dir, manifest, kind=None):
    header = manifest.get("header", {})
    name = manifest_display_name(Path(pack_dir), manifest)
    uuid = header.get("uuid", "")[:8]
    base = f"{clean_name(name)}_{uuid}" if uuid else clean_name(name)
    if kind in ("rp", "bp"):
        return f"{base}-{'RP' if kind == 'rp' else 'BP'}"
    return base


def manifest_dependencies(manifest):
    deps = []
    for dep in manifest.get("dependencies", []):
        uuid = dep.get("uuid")
        version = dep.get("version")
        if uuid:
            deps.append({"uuid": uuid, "version": version})
    return deps


def check_dependencies(installed, available_packs=None):
    """Return manifest dependencies not found in installed/server packs."""
    available_packs = available_packs or installed
    installed_ids = {p.get("pack_id") for p in available_packs if p.get("pack_id")}
    missing = []
    for pack in installed:
        for dep in pack.get("dependencies", []):
            if dep["uuid"] not in installed_ids:
                missing.append((pack, dep))
    return missing


def archive_stem_from_manifest(manifest_name: str) -> str:
    parent = Path(manifest_name).parent
    return parent.name if str(parent) not in ("", ".") else "pack"

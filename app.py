#!/usr/bin/env python3
import argparse
import io
import json
import logging
import os
import shutil
import sys
import tarfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional
import re

PACK_EXTS = {".mcpack", ".mcaddon", ".mctemplate", ".zip"}
TAR_EXTS = {".tar.gz", ".tgz", ".tar.bz2"}      # supported tar formats
WORLD_MARKERS = {"level.dat", "levelname.txt"}
DRY_RUN = False
MAX_ARCHIVE_MB = 500
UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE
)


def is_tar_file(path: Path) -> bool:
    """Check whether path is a tar file, including double extensions like .tar.gz."""
    name = path.name.lower()
    return any(name.endswith(ext) for ext in TAR_EXTS)


def is_pack_file(path: Path) -> bool:
    """Check whether path is a supported pack/archive file."""
    return path.suffix.lower() in PACK_EXTS or is_tar_file(path)



def is_within_dir(base: Path, target: Path) -> bool:
    """Check that target stays inside base after resolving paths."""
    try:
        return os.path.commonpath([str(base.resolve()), str(target.resolve())]) == str(base.resolve())
    except ValueError:
        return False


def safe_child_path(base: Path, name: str, label: str) -> Path:
    """Build a child path that cannot escape base."""
    target = base / name
    if not is_within_dir(base, target):
        raise RuntimeError(f"Path traversal blocked: {label}")
    return target


def safe_world_name(name: str) -> str:
    """Validate world names so they cannot become path traversal."""
    cleaned = name.strip()
    if not cleaned:
        raise RuntimeError("World name cannot be empty.")
    if Path(cleaned).is_absolute() or Path(cleaned).name != cleaned:
        raise RuntimeError(f"Unsafe world name: {name}")
    if cleaned in {".", ".."}:
        raise RuntimeError(f"Unsafe world name: {name}")
    return cleaned


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


def manifest_module_types(manifest):
    return [str(m.get("type", "unknown")) for m in manifest.get("modules", [])]


def detect_pack_kinds(manifest):
    types = manifest_module_types(manifest)
    kinds = []
    if "resources" in types:
        kinds.append("rp")
    if "data" in types or "script" in types:
        kinds.append("bp")
    return kinds


def manifest_kind_label(manifest) -> str:
    labels = [kind_name(kind) for kind in detect_pack_kinds(manifest)]
    types = manifest_module_types(manifest)
    if "world_template" in types:
        labels.append("World Template")
    return ", ".join(labels) if labels else "Unknown"


def strip_bedrock_formatting(text: str) -> str:
    """Remove Minecraft formatting codes such as §l and §6 from display names."""
    return re.sub(r"§.", "", str(text or ""))


def clean_name(name):
    cleaned = "".join(c if c.isalnum() or c in "._- " else "_" for c in strip_bedrock_formatting(name)).strip()
    return cleaned.replace(" ", "_") or "pack"


def clean_pack_title(name: str) -> str:
    """Return a readable pack title without color codes, comments, version text, or BP/RP suffixes."""
    text = strip_bedrock_formatting(name).replace("_", " ")
    text = re.sub(r"\s+#{2,}.*$", "", text).strip()
    text = re.sub(r"[\[\(]\s*v?\d+(?:\.\d+){1,3}\s*[\]\)]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[\[\(]\s*(bp|rp|resource|resources|behavior|behaviour|texture|textures|addon|add-on|pack|world)\s*[\]\)]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[\[\(]\s*[\]\)]", " ", text)
    text = re.sub(r"\b(resource|resources|behavior|behaviour|texture|textures)\s+pack\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\badd-?on\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:\s*[-–—|:]\s*)?\b(bp|rp|resource|resources|behavior|behaviour|texture|textures)\s+(\d+)\b", r" \2", text, flags=re.IGNORECASE)
    text = re.sub(r"\bv?\d+(?:\.\d+){1,3}\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:\s*[-–—|:]\s*)?\b(bp|rp|resource|resources|behavior|behaviour|texture|textures|world)\b\s*", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" -–—_|:")
    return text or "Pack"


def safe_folder_component(name: str) -> str:
    """Sanitize a readable folder name without turning spaces into underscores."""
    cleaned = "".join(c if c.isalnum() or c in " ._-&()[]" else " " for c in strip_bedrock_formatting(name))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._-&")
    return cleaned or "Pack"


def source_stem(source_name) -> str:
    """Return an addon archive stem without supported archive extensions."""
    name = Path(str(source_name).split("!", 1)[0]).name
    lower_name = name.lower()
    for ext in sorted(TAR_EXTS | PACK_EXTS, key=len, reverse=True):
        if lower_name.endswith(ext):
            return name[:-len(ext)]
    return Path(name).stem


def source_title(source_name) -> str:
    """Build a readable addon title from the archive file name."""
    text = source_stem(source_name)
    text = re.sub(r"\s+#{2,}.*$", "", text).strip()
    text = re.sub(r"\bv?\d+(?:\.\d+){1,3}\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s*[-–—]+\s*", " ", text)
    text = re.sub(r"\b(?:r\d+[a-z0-9]*|r?[a-z]+\d+[a-z0-9]*)\b\s*$", "", text, flags=re.IGNORECASE)
    return clean_pack_title(text)


def title_compare_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def title_for_pack(pack_dir: Path, manifest: dict, source_name=None) -> str:
    """Choose the best readable title, preferring the addon file name."""
    manifest_title = clean_pack_title(manifest_display_name(pack_dir, manifest))
    if not source_name:
        return manifest_title

    file_title = source_title(source_name)
    if not file_title or file_title == "Pack":
        return manifest_title

    manifest_key = title_compare_key(manifest_title)
    file_key = title_compare_key(file_title)
    if manifest_key.startswith(file_key + " "):
        return manifest_title
    if file_key.startswith(manifest_key + " "):
        extra = file_key[len(manifest_key):].strip()
        if re.fullmatch(r"r?[a-z0-9]*\d+[a-z0-9]*", extra):
            return manifest_title
    return file_title


def manifest_version_label(manifest: dict) -> str:
    try:
        version = version_array(manifest.get("header", {}).get("version"))
        while len(version) > 1 and version[-1] == 0:
            version.pop()
        return format_version(version)
    except Exception:
        return format_version(manifest.get("header", {}).get("version"))


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


def pack_folder_name(pack_dir, manifest, kind=None, source_name=None):
    name = title_for_pack(Path(pack_dir), manifest, source_name)
    version = manifest_version_label(manifest)
    suffix = f" v{version}" if version != "unknown" else ""
    if kind in ("rp", "bp"):
        return safe_folder_component(f"{name}{suffix} {kind.upper()}")
    return safe_folder_component(f"{name}{suffix}")


def unique_world_pack_folder(base: Path, desired_name: str, current: Path) -> Path:
    """Return a local world pack folder path that does not collide."""
    candidate = safe_child_path(base, desired_name, desired_name)
    if candidate.resolve() == current.resolve() or not candidate.exists():
        return candidate
    index = 2
    while True:
        candidate_name = f"{desired_name}-{index}"
        candidate = safe_child_path(base, candidate_name, candidate_name)
        if not candidate.exists():
            return candidate
        index += 1


def normalize_world_local_pack_folders(world_dir: Path, source_name=None):
    """Rename imported world-local BP/RP folders from generic bp0/rp0 to readable pack names."""
    if DRY_RUN:
        return []
    renamed = []
    for kind, folder_name in (("bp", "behavior_packs"), ("rp", "resource_packs")):
        base = world_dir / folder_name
        if not base.exists():
            continue
        for pack_dir in sorted([p for p in base.iterdir() if p.is_dir()], key=lambda p: p.name.lower()):
            manifest_path = pack_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = load_json(manifest_path)
            except Exception as e:
                log.warning("Cannot read world-local pack manifest %s: %s", manifest_path, e)
                continue
            if kind not in detect_pack_kinds(manifest):
                continue
            desired_name = pack_folder_name(pack_dir, manifest, kind, source_name)
            dest = unique_world_pack_folder(base, desired_name, pack_dir)
            if dest.resolve() == pack_dir.resolve():
                continue
            log.info("Rename world-local pack folder: %s -> %s", pack_dir, dest)
            pack_dir.rename(dest)
            renamed.append({"kind": kind, "old": pack_dir.name, "new": dest.name})
    return renamed


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

# ── Logging setup ────────────────────────────────────────────────────────────
log = logging.getLogger("bedrock_addon")


def setup_logging(dry_run: bool = False) -> Optional[Path]:
    """Initialize logging to file and stderr."""
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    log_file = Path("addonInstaller.log")
    log_file_resolved = log_file.resolve()

    if dry_run:
        for handler in list(log.handlers):
            if isinstance(handler, logging.FileHandler):
                log.removeHandler(handler)
                handler.close()
    else:
        has_file_handler = any(
            isinstance(handler, logging.FileHandler)
            and Path(handler.baseFilename).resolve() == log_file_resolved
            for handler in log.handlers
        )
        if not has_file_handler:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            log.addHandler(fh)

    has_stderr_handler = any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        and handler.stream is sys.stderr
        for handler in log.handlers
    )
    if not has_stderr_handler:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.WARNING)
        sh.setFormatter(fmt)
        log.addHandler(sh)

    log.info("=" * 60)
    log.info("Bedrock Addon Setup started")
    log.info("Platform: %s | Python: %s", sys.platform, sys.version.split()[0])
    return None if dry_run else log_file


# ── Color utilities ──────────────────────────────────────────────────────────
_COLOR_ENABLED = False


def _enable_colors() -> bool:
    """Enable ANSI color. On Windows, enable VT mode through ctypes."""
    global _COLOR_ENABLED
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            # GetStdHandle(-11) = STD_OUTPUT_HANDLE
            handle = k32.GetStdHandle(-11)
            if not k32.SetConsoleMode(handle, 7):
                return False
        except Exception:
            return False
    _COLOR_ENABLED = True
    log.info("Terminal color: enabled")
    return True


def _c(code: str, text: str) -> str:
    """Wrap text with ANSI escape code when color is enabled."""
    return f"\033[{code}m{text}\033[0m" if _COLOR_ENABLED else text

def c_green(t: str)  -> str: return _c("92", t)
def c_yellow(t: str) -> str: return _c("93", t)
def c_red(t: str)    -> str: return _c("91", t)
def c_cyan(t: str)   -> str: return _c("96", t)
def c_blue(t: str)   -> str: return _c("94", t)
def c_magenta(t: str)-> str: return _c("95", t)
def c_bold(t: str)   -> str: return _c("1",  t)
def c_gray(t: str)   -> str: return _c("90", t)
def c_ok(t: str)     -> str: return c_green(f"✓ {t}")
def c_warn(t: str)   -> str: return c_yellow(f"⚠ {t}")
def c_err(t: str)    -> str: return c_red(f"✗ {t}")
def c_info(t: str)   -> str: return c_cyan(f"→ {t}")


def strip_ansi(text: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", str(text))


def display_len(text) -> int:
    return len(strip_ansi(str(text)))


def terminal_width(default: int = 88) -> int:
    return max(60, min(110, shutil.get_terminal_size((default, 20)).columns))


def clip_text(text, max_len: int) -> str:
    text = str(text)
    if display_len(text) <= max_len:
        return text
    plain = strip_ansi(text)
    return plain[:max(0, max_len - 1)] + "…"


def pad_ansi(text, width: int) -> str:
    return str(text) + " " * max(0, width - display_len(text))


def ui_badge(text: str, kind: str = "info") -> str:
    label = f" {str(text).strip()} "
    if kind in ("ok", "success"):
        return c_green(f"[{label}]")
    if kind in ("warn", "warning"):
        return c_yellow(f"[{label}]")
    if kind in ("err", "error", "danger"):
        return c_red(f"[{label}]")
    if kind == "muted":
        return c_gray(f"[{label}]")
    if kind == "accent":
        return c_magenta(f"[{label}]")
    return c_cyan(f"[{label}]")


def ui_rule(title: str = "") -> str:
    width = terminal_width()
    if not title:
        return c_gray("─" * width)
    line_len = max(8, width - display_len(title) - 4)
    return f"{c_bold(title)} {c_gray('─' * line_len)}"


def c_divider(t: str = "") -> str:
    return f"\n{ui_rule(t)}"


def ui_panel(title: str, rows=None, subtitle: str = "", kind: str = "info") -> None:
    rows = rows or []
    width = terminal_width()
    border_color = {"ok": c_green, "warn": c_yellow, "err": c_red, "danger": c_red}.get(kind, c_cyan)
    print(border_color("╭" + "─" * (width - 2) + "╮"))
    print(border_color("│") + pad_ansi(f" {c_bold(title)}", width - 2) + border_color("│"))
    if subtitle:
        print(border_color("│") + pad_ansi(f" {c_gray(clip_text(subtitle, width - 4))}", width - 2) + border_color("│"))
    if rows:
        print(border_color("├" + "─" * (width - 2) + "┤"))
    for row in rows:
        if isinstance(row, tuple):
            label, value = row
            content = f" {c_gray(str(label) + ':')} {value}"
        else:
            content = f" {row}"
        content = clip_text(content, width - 4)
        print(border_color("│") + pad_ansi(content, width - 2) + border_color("│"))
    print(border_color("╰" + "─" * (width - 2) + "╯"))


def ui_step(number: int, title: str, note: str = "") -> None:
    """Print a consistent step header."""
    print()
    print(f"{ui_badge(f'STEP {number}', 'accent')} {c_bold(title)}")
    if note:
        print(f"  {c_gray(note)}")
    else:
        print(f"  {c_gray('─' * max(12, terminal_width() - 4))}")


def ui_kv(label: str, value) -> None:
    """Print compact key/value rows for readable metadata."""
    print(f"  {c_gray(str(label) + ':')} {value}")


def ui_option(number: str, label: str, hint: str = "") -> None:
    """Print a numbered menu option."""
    key_kind = "danger" if str(number) == "0" else "info"
    key = ui_badge(str(number), key_kind)
    suffix = f"  {c_gray(hint)}" if hint else ""
    print(f"  {key} {label}{suffix}")


def ui_status(kind: str, text: str) -> None:
    """Print a high-signal status line."""
    badge_map = {"ok": "OK", "warn": "WARN", "err": "ERROR", "info": "INFO"}
    badge_kind = {"ok": "ok", "warn": "warn", "err": "danger", "info": "info"}.get(kind, "info")
    print(f"  {ui_badge(badge_map.get(kind, 'INFO'), badge_kind)} {text}")


def ui_phase(label: str, detail: str = "") -> None:
    """Print an archive processing phase."""
    suffix = f" {c_gray(detail)}" if detail else ""
    print(f"\n  {ui_badge('PHASE', 'accent')} {c_bold(label)}{suffix}")


def ui_subitem(label: str, value: str = "") -> None:
    """Print an indented detail row inside a phase."""
    suffix = f" {value}" if value else ""
    print(f"    {c_gray('•')} {label}{suffix}")


def ui_help(*items: str) -> None:
    print(f"  {c_gray(' · '.join(items))}")


def ui_pause(prompt: str = "Press Enter to continue...") -> None:
    input(f"  {c_gray(prompt)}")


def ui_checkbox_row(index: int, text: str, selected: bool = False, cursor: bool = False, hint: str = "", order: Optional[int] = None) -> None:
    order_prefix = f"{order:<2}" if order is not None else "  "
    pointer = c_cyan("›") if cursor else " "
    check = c_green("[✓]") if selected else c_gray("[ ]")
    number = ui_badge(str(index), "muted")
    row_text = c_bold(text) if cursor else text
    suffix = ""
    if hint:
        clean_hint = strip_ansi(hint)
        lowered = clean_hint.lower()
        if "already installed" in lowered:
            clean_hint = clean_hint.replace("(already installed)", "").strip()
            suffix = f" {c_gray(clean_hint)} {ui_badge('INSTALLED', 'warn')}"
        elif "partially installed" in lowered:
            clean_hint = clean_hint.replace("(partially installed)", "").strip()
            suffix = f" {c_gray(clean_hint)} {ui_badge('PARTIAL', 'warn')}"
        else:
            suffix = f" {c_gray(clean_hint)}"
    line = f"  {order_prefix}{pointer} {check} {number} {row_text}{suffix}"
    if cursor:
        print(_c("7", pad_ansi(strip_ansi(line), terminal_width() - 2)))
    else:
        print(line)


def ui_menu(title: str, rows=None, subtitle: str = "") -> None:
    print(c_divider(title))
    if subtitle:
        print(f"  {c_gray(subtitle)}")
    for row in rows or []:
        ui_kv(*row)

def short_path(path, max_len: int = 72) -> str:
    """Return a compact path for console output."""
    text = str(path)
    if len(text) <= max_len:
        return text
    return "…" + text[-(max_len - 1):]


def clear_screen() -> None:
    """Clear the interactive picker screen without appending repeated menus."""
    if not sys.stdout.isatty():
        return
    if sys.platform == "win32" and not _COLOR_ENABLED:
        os.system("cls")
        return
    print("\033[2J\033[H", end="")


def plural(count: int, singular: str, plural_word=None) -> str:
    word = singular if count == 1 else (plural_word or f"{singular}s")
    return f"{count} {word}"


def ui_banner(log_file, force_delete=False) -> None:
    """Print the app banner and current runtime mode."""
    rows = [
        ("Author", "@zoxervy"),
        ("Safety", "Backups before overwrite · safe archive extraction"),
        ("Log", log_file.resolve() if log_file else "disabled in dry-run mode"),
    ]
    modes = []
    if DRY_RUN:
        modes.append(ui_badge("DRY-RUN", "warn"))
    if force_delete:
        modes.append(ui_badge("FORCE DELETE", "danger"))
    if modes:
        rows.append(("Mode", " ".join(modes)))
    print()
    ui_panel(
        "Bedrock Addon Installer",
        rows,
        "Install, enable, reorder, and remove Minecraft Bedrock addons",
        "warn" if force_delete else "info",
    )


# ── Progress bar ─────────────────────────────────────────────────────────────
def print_progress(current: int, total: int, label: str = "", bar_width: int = 28, show_label: bool = False) -> None:
    """Show a progress bar on the same line using carriage-return overwrite."""
    if total == 0:
        return
    width = terminal_width()
    suffix_width = 34 if show_label and label else 18
    bar_width = max(12, min(bar_width, width - suffix_width))
    pct = current / total
    done = int(bar_width * pct)
    bar = "\u2588" * done + "\u2591" * (bar_width - done)
    bar_str = c_cyan(bar) if _COLOR_ENABLED else bar
    suffix = ""
    if show_label and label:
        label_str = clip_text(label, max(12, width - bar_width - 24))
        suffix = f" {c_gray(label_str) if _COLOR_ENABLED else label_str}"
    line = f"    {ui_badge('WORK', 'accent')} [{bar_str}] {pct:5.1%} ({current}/{total}){suffix}"
    sys.stdout.write("\r" + line + " " * max(0, width - display_len(line)))
    sys.stdout.flush()
    if current >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()


class UserExit(KeyboardInterrupt):
    """Raised when the user chooses an explicit exit/cancel option."""


EXIT_CHOICES = {"0", "q", "quit", "exit", "cancel", "x"}


def is_exit_choice(value) -> bool:
    return str(value or "").strip().lower() in EXIT_CHOICES


def raise_if_exit(value) -> None:
    if is_exit_choice(value):
        raise UserExit


def ask(prompt, default=None, allow_exit=True):
    suffix = f" {c_gray('[' + str(default) + ']')}" if default else ""
    try:
        value = input(f"  {prompt}{suffix}: ").strip()
    except EOFError:
        raise UserExit
    if value and allow_exit:
        raise_if_exit(value)
    return value or default


def yes_no(prompt, default=True, allow_exit=True):
    d = "Y/n/q" if default else "y/N/q"
    while True:
        try:
            value = input(f"  {prompt} {c_gray('[' + d + ']')}: ").strip().lower()
        except EOFError:
            raise UserExit
        if value and allow_exit:
            raise_if_exit(value)
        if not value:
            return default
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        ui_status("warn", "Answer y/n, or q to cancel.")


def action(text: str) -> None:
    if DRY_RUN:
        msg = f"  {ui_badge('DRY-RUN', 'warn')} {text}"
    else:
        msg = f"  {ui_badge('INFO', 'info')} {text}" if _COLOR_ENABLED else text
    print(msg)
    log.info(text)


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def find_server_root_for_path(path: Path) -> Optional[Path]:
    """Find the nearest Bedrock server root for a path being backed up."""
    start = path if path.is_dir() else path.parent
    for candidate in [start, *start.parents]:
        if (candidate / "server.properties").exists() or (candidate / server_binary_name()).exists():
            return candidate
    return None


def backup_category_for_path(path: Path, server_dir: Optional[Path]) -> str:
    if server_dir:
        try:
            first = path.resolve().relative_to(server_dir.resolve()).parts[0].lower()
        except (ValueError, IndexError):
            first = ""
        if first == "behavior_packs":
            return "bp"
        if first == "resource_packs":
            return "rp"
        if first == "worlds":
            return "worlds"
    if path.name == "server.properties":
        return "config"
    return "other"


def backup_item_name(path: Path, server_dir: Optional[Path], category: str) -> str:
    if server_dir and category in ("worlds", "config"):
        try:
            rel_parts = path.resolve().relative_to(server_dir.resolve()).parts
            base = "__".join(clean_name(part) for part in rel_parts)
        except ValueError:
            base = clean_name(path.name)
    else:
        base = clean_name(path.name)
    return f"{base}.bak-{timestamp()}"


def backup_root_for_path(path: Path) -> tuple[Path, Optional[Path], str]:
    server_dir = find_server_root_for_path(path)
    server_name = clean_name(server_dir.name) if server_dir else "misc"
    root = Path(__file__).resolve().parent / ".temp-addonInstaller" / "backups"
    server_root = safe_child_path(root, server_name, server_name)
    category = backup_category_for_path(path, server_dir)
    category_root = safe_child_path(server_root, category, category)
    return category_root, server_dir, category


def unique_backup_path(path: Path) -> Path:
    """Return a centralized backup path that will not overwrite an existing backup."""
    backup_dir, server_dir, category = backup_root_for_path(path)
    backup_name = backup_item_name(path, server_dir, category)
    backup = safe_child_path(backup_dir, backup_name, backup_name)
    if not backup.exists():
        return backup
    index = 1
    while True:
        candidate_name = f"{backup_name}-{index}"
        candidate = safe_child_path(backup_dir, candidate_name, candidate_name)
        if not candidate.exists():
            return candidate
        index += 1


def backup_path(path: Path) -> Path:
    return unique_backup_path(path)


def uninstall_backup_path(server_dir: Path, pack_path: Path, kind: str) -> Path:
    """Build a centralized uninstall backup path grouped by server and pack kind."""
    backup_root = Path(__file__).resolve().parent / ".temp-addonInstaller" / "backups"
    server_root = safe_child_path(backup_root, clean_name(server_dir.name), server_dir.name)
    kind_root = safe_child_path(server_root, kind, kind)
    backup_name = f"{clean_name(pack_path.name)}.bak-{timestamp()}"
    backup = safe_child_path(kind_root, backup_name, backup_name)
    if not backup.exists():
        return backup
    index = 1
    while True:
        candidate_name = f"{backup_name}-{index}"
        candidate = safe_child_path(kind_root, candidate_name, candidate_name)
        if not candidate.exists():
            return candidate
        index += 1

def backup_existing(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    backup = backup_path(path)
    log.info("Backup: %s -> %s", path, backup)
    if not DRY_RUN:
        backup.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(path, backup)
        else:
            shutil.copy2(path, backup)
    return backup


def read_server_level_name(server_dir: Path) -> Optional[str]:
    props = server_dir / "server.properties"
    if not props.exists():
        return None
    for line in props.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("level-name="):
            return line.split("=", 1)[1].strip() or None
    return None


def set_server_level_name(server_dir: Path, world_name: str):
    """Set server.properties level-name and return rollback backup info."""
    props = server_dir / "server.properties"
    content = props.read_text(encoding="utf-8", errors="ignore") if props.exists() else ""
    lines = content.splitlines()
    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith("level-name="):
            new_lines.append(f"level-name={world_name}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"level-name={world_name}")
    return props, write_text(props, "\n".join(new_lines) + "\n")


def server_binary_name() -> str:
    return "bedrock_server.exe" if sys.platform == "win32" else "bedrock_server"


def server_binary_path(server_dir: Path) -> Path:
    return server_dir / server_binary_name()


def validate_server_dir(server_dir):
    missing = []
    if not (server_dir / "server.properties").exists():
        missing.append("server.properties")
    binary = server_binary_path(server_dir)
    if not binary.exists():
        missing.append(binary.name)
    if missing:
        raise RuntimeError(f"This folder is not a valid/complete Bedrock server. Missing: {', '.join(missing)}")


def windows_file_version(path: Path) -> Optional[str]:
    """Read Windows executable file version without starting the server."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        version_dll = ctypes.windll.version
        handle = wintypes.DWORD()
        size = version_dll.GetFileVersionInfoSizeW(str(path), ctypes.byref(handle))
        if not size:
            return None

        buffer = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None

        value = ctypes.c_void_p()
        value_len = wintypes.UINT()
        if not version_dll.VerQueryValueW(buffer, "\\", ctypes.byref(value), ctypes.byref(value_len)):
            return None

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        info = ctypes.cast(value, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        if info.dwSignature != 0xFEEF04BD:
            return None
        parts = [
            info.dwFileVersionMS >> 16,
            info.dwFileVersionMS & 0xFFFF,
            info.dwFileVersionLS >> 16,
            info.dwFileVersionLS & 0xFFFF,
        ]
        if not any(parts):
            return None
        return ".".join(map(str, parts))
    except Exception as e:
        log.debug("Cannot read Windows file version from %s: %s", path, e)
        return None


def read_server_version_file(server_dir: Path) -> Optional[str]:
    """Read a simple version marker file when a server bundle provides one."""
    version_pattern = re.compile(r"\b\d+\.\d+(?:\.\d+){1,2}\b")
    for file_name in ("version.txt", "bedrock_server.version", "server_version.txt"):
        path = server_dir / file_name
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        match = version_pattern.search(text)
        if match:
            return match.group(0)
    return None


def scan_binary_for_version(path: Path) -> Optional[str]:
    """Best-effort fallback for binaries that expose a readable version string."""
    version_pattern = re.compile(rb"\b1\.\d{1,3}\.\d{1,3}(?:\.\d{1,3})?\b")
    matches = set()
    try:
        with path.open("rb") as src:
            tail = b""
            for _ in range(64):
                chunk = src.read(256 * 1024)
                if not chunk:
                    break
                blob = tail + chunk
                matches.update(match.decode("ascii") for match in version_pattern.findall(blob))
                tail = blob[-32:]
    except OSError as e:
        log.debug("Cannot scan server binary version from %s: %s", path, e)
        return None

    if not matches:
        return None
    return sorted(matches, key=lambda value: [int(part) for part in value.split(".")])[-1]


def detect_server_version(server_dir: Path) -> Optional[str]:
    """Return the Bedrock server version when it can be detected safely."""
    binary = server_binary_path(server_dir)
    return (
        read_server_version_file(server_dir)
        or windows_file_version(binary)
        or scan_binary_for_version(binary)
    )


def format_server_version(server_dir: Path) -> str:
    version = detect_server_version(server_dir)
    return f"v{version}" if version else "unknown"


def looks_like_server_dir(server_dir: Path) -> bool:
    """Return True for folders worth showing in the server picker."""
    return (server_dir / "server.properties").exists() or server_binary_path(server_dir).exists()


def is_visible_folder(path: Path) -> bool:
    """Return True for folders worth showing in interactive lists."""
    return path.is_dir() and not path.name.startswith(".") and path.name != "__pycache__"


def visible_folders(parent: Path) -> list[Path]:
    if not parent.exists():
        return []
    return sorted([p for p in parent.iterdir() if is_visible_folder(p)], key=lambda p: p.name.lower())

def addon_group_key_from_name(name: str, fallback: str = "") -> str:
    """Return a display-oriented key that groups BP/RP sides of one addon."""
    text = re.sub(r"§.", "", str(name)).lower()
    text = re.sub(
        r"[\[(]\s*(bp|rp|resource|resources|behavior|behaviour|texture|textures|addon|add-on|pack|world)\s*[\])]",
        " ",
        text,
    )
    text = re.sub(
        r"\s+-\s+(resource|resources|texture|textures|behavior|behaviour|addon|add-on|pack)\b.*$",
        " ",
        text,
    )
    text = re.sub(r"\b(resource|resources|behavior|behaviour|texture|textures)\s+pack\b", " ", text)
    text = re.sub(r"\b(bp|rp|world)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_")
    version_prefix = re.match(r"^(.+?\b\d+(?:\.\d+){1,3})\b", text)
    if version_prefix:
        text = version_prefix.group(1).strip(" -_")
    return text or fallback


def addon_count_key(pack_dir: Path, manifest: dict) -> str:
    header = manifest.get("header", {})
    name = manifest_display_name(pack_dir, manifest) or pack_dir.name
    return addon_group_key_from_name(name, header.get("uuid") or pack_dir.name.lower())


def server_addon_count_hint(server_dir: Path):
    """Return a short installed-addon hint for folders that look like Bedrock servers."""
    if not (server_dir / "server.properties").exists() or not server_binary_path(server_dir).exists():
        return ""

    addons = set()
    for folder_name in ("resource_packs", "behavior_packs"):
        base = server_dir / folder_name
        if not base.exists():
            continue
        for pack_dir in base.iterdir():
            if not (
                pack_dir.is_dir()
                and not _is_builtin_pack(pack_dir.name)
                and (pack_dir / "manifest.json").exists()
            ):
                continue
            try:
                manifest = load_json(pack_dir / "manifest.json")
            except Exception:
                addons.add(pack_dir.name.lower())
                continue
            addons.add(addon_count_key(pack_dir, manifest))
    return f"({plural(len(addons), 'addon')} installed)"


def print_server_info(server_dir: Path) -> None:
    """Show selected server metadata before choosing an action."""
    rows = [
        ("Folder", server_dir),
        ("Binary", server_binary_path(server_dir).name),
        ("Version", format_server_version(server_dir)),
    ]
    level_name = read_server_level_name(server_dir)
    if level_name:
        rows.append(("Level", level_name))
    ui_panel("Server info", rows, kind="ok")


def choose_server_dir():
    """Choose a Bedrock server folder through an interactive menu."""
    current = Path.cwd().resolve()
    while True:
        ui_menu("Server folder", [("Current", current)])
        ui_option("1", "Use current folder", f"{current.name}/")
        ui_option("2", "Browse folders here")
        ui_option("3", "Enter custom path")
        ui_option("0", "Exit")
        choice = ask("Select option", "1")

        try:
            if choice == "1":
                server_dir = current
            elif choice == "2":
                folders = [folder for folder in visible_folders(current) if looks_like_server_dir(folder)]
                if not folders:
                    ui_status("warn", "No Bedrock server folders found here.")
                    ui_kv("Expected", "folder with server.properties or bedrock_server(.exe)")
                    continue
                print(c_divider("Available server folders"))
                for idx, folder in enumerate(folders, 1):
                    ui_option(str(idx), folder.name, server_addon_count_hint(folder))
                raw = ask("Select folder")
                try:
                    index = int(raw)
                except (TypeError, ValueError):
                    ui_status("warn", "Choice must be a number.")
                    continue
                if index < 1 or index > len(folders):
                    ui_status("warn", "Invalid folder choice.")
                    continue
                server_dir = folders[index - 1]
            elif choice == "3":
                manual = ask("Bedrock server folder path", str(current))
                server_dir = Path(manual).expanduser().resolve()
            else:
                ui_status("warn", "Choice must be 0, 1, 2, or 3.")
                continue

            if not server_dir.exists():
                ui_status("err", f"Server folder does not exist: {server_dir}")
                continue
            validate_server_dir(server_dir)
            return server_dir
        except RuntimeError as e:
            ui_status("err", f"Error: {e}")
            ui_kv("Expected", "server.properties and bedrock_server(.exe)")
            ui_kv("Tip", "Use option 3 if the server is elsewhere.")


def validate_archive(archive: Path) -> None:
    """Validate archive size and integrity before processing."""
    if not archive.exists():
        raise RuntimeError(f"File does not exist: {archive}")
    size_mb = archive.stat().st_size / (1024 * 1024)
    log.info("Archive: %s (%.1f MB)", archive.name, size_mb)
    if size_mb > MAX_ARCHIVE_MB:
        raise RuntimeError(
            f"File is too large: {size_mb:.1f} MB (limit {MAX_ARCHIVE_MB} MB). "
            f"Change MAX_ARCHIVE_MB if needed."
        )
    if is_tar_file(archive):
        if not tarfile.is_tarfile(archive):
            raise RuntimeError(f"Not a valid tar file: {archive}")
    elif not zipfile.is_zipfile(archive):
        raise RuntimeError(f"Not a valid zip/mcpack file: {archive}")
    log.info("Archive valid: %s (%.1f MB)", archive.name, size_mb)


def check_disk_space(src, dest_parent):
    """Ensure the destination disk has enough free space to copy src."""
    if DRY_RUN:
        return
    needed = sum(f.stat().st_size for f in Path(src).rglob("*") if f.is_file())
    try:
        free = shutil.disk_usage(dest_parent).free
    except OSError:
        log.warning("Cannot check disk space at %s, skipping.", dest_parent)
        return
    needed_mb = needed / (1024 * 1024)
    free_mb = free / (1024 * 1024)
    log.info("Disk check: need %.1f MB, available %.1f MB at %s", needed_mb, free_mb, dest_parent)
    if needed > free:
        raise RuntimeError(
            f"Not enough disk space: need {needed_mb:.1f} MB, "
            f"available {free_mb:.1f} MB at {dest_parent}"
        )


def safe_extract(zip_path: Path, dest: Path) -> None:
    """Extract ZIP with path traversal protection (zip slip)."""
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        members = z.infolist()
        files = [member for member in members if not member.is_dir()]
        total = len(files)
        copied = 0
        log.info("Extract zip: %s (%d files)", zip_path.name, total)
        for member in members:
            safe_name = member.filename.replace("/", os.sep).replace("\\", os.sep)
            target = safe_child_path(dest, safe_name, member.filename)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            copied += 1
            if total and (copied == total or copied % 25 == 0):
                print_progress(copied, total, Path(member.filename).name)
        pass  # progress bar already writes its own newline


def safe_extract_tar(tar_path: Path, dest: Path) -> None:
    """Extract TAR (.tar.gz/.tgz/.tar.bz2) with path traversal protection."""
    dest = dest.resolve()
    with tarfile.open(tar_path) as tf:
        members = tf.getmembers()
        files = [member for member in members if member.isfile()]
        total = len(files)
        copied = 0
        log.info("Extract tar: %s (%d files)", tar_path.name, total)
        for member in members:
            if member.issym() or member.islnk():
                raise RuntimeError(f"Unsafe tar member blocked (link): {member.name}")
            if not (member.isfile() or member.isdir()):
                raise RuntimeError(f"Unsafe tar member blocked: {member.name}")
            safe_child_path(dest, member.name, member.name)
        for member in members:
            target = safe_child_path(dest, member.name, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            src = tf.extractfile(member)
            if src is None:
                raise RuntimeError(f"Cannot extract tar member: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            copied += 1
            if total and (copied == total or copied % 25 == 0):
                print_progress(copied, total, Path(member.name).name)
        pass  # progress bar already writes its own newline



def copytree_with_progress(src: Path, dest: Path) -> None:
    """Copy src directory to dest with disk check and progress bar."""
    if not DRY_RUN:
        check_disk_space(src, dest.parent)
    log.info("Copy: %s → %s", src, dest)
    if not DRY_RUN:
        # Collect file list first for the progress bar
        src_files = [f for f in Path(src).rglob("*") if f.is_file()]
        total = len(src_files)
        counter = [0]

        def _copy_fn(s: str, d: str) -> str:
            result = shutil.copy2(s, d)
            counter[0] += 1
            print_progress(counter[0], total, Path(s).name)
            return result

        shutil.copytree(src, dest, copy_function=_copy_fn)
        if total == 0:
            pass  # empty directory, no progress to show
        log.info("Copy complete: %s → %s (%d files)", src, dest, total)


def safe_copytree(src: Path, dest: Path):
    """Copy src directory to dest with backup, disk check, and progress bar."""
    backup = None
    if dest.exists():
        if yes_no(f"Folder {dest.name} already exists. Replace?", True):
            backup = backup_existing(dest)
            log.info("Remove: %s", dest)
            if not DRY_RUN:
                shutil.rmtree(dest)
        else:
            raise RuntimeError("Cancelled because the pack/world folder already exists.")
    copytree_with_progress(src, dest)
    return backup


def replace_copytree(src: Path, dest: Path):
    """Replace an existing directory after caller has confirmed the overwrite."""
    backup = backup_existing(dest)
    log.info("Remove: %s", dest)
    if not DRY_RUN and dest.exists():
        shutil.rmtree(dest)
    copytree_with_progress(src, dest)
    return backup


def write_text(path, content):
    backup = None
    if path.exists():
        backup = backup_existing(path)
    log.info("Write: %s", path)
    if not DRY_RUN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        log.debug("Write file: %s (%d bytes)", path, len(content.encode()))
    return backup


def rollback_path(path: Path, backup=None) -> None:
    """Remove a newly written path and restore its previous backup if one exists."""
    if DRY_RUN:
        return
    if path.exists():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    if backup and Path(backup).exists():
        shutil.move(str(backup), str(path))

def rollback_install(installed, imported_worlds, config_changes) -> None:
    """Best-effort rollback for a cancelled or failed install transaction."""
    if DRY_RUN:
        return
    ui_status("warn", "Rolling back install changes.")
    for path, backup in reversed(config_changes):
        try:
            rollback_path(Path(path), Path(backup) if backup else None)
            ui_status("ok", f"Rolled back config: {path}")
        except Exception as e:
            ui_status("err", f"Rollback failed for config {path}: {e}")
            log.warning("Rollback failed for config %s: %s", path, e)
    for world in reversed(imported_worlds):
        path = Path(world["path"])
        backup = Path(world["backup"]) if world.get("backup") else None
        try:
            rollback_path(path, backup)
            ui_status("ok", f"Rolled back world: {path}")
        except Exception as e:
            ui_status("err", f"Rollback failed for world {path}: {e}")
            log.warning("Rollback failed for world %s: %s", path, e)
    for pack in reversed(installed):
        path = Path(pack["path"])
        backup = Path(pack["backup"]) if pack.get("backup") else None
        try:
            rollback_path(path, backup)
            ui_status("ok", f"Rolled back pack: {path}")
            replaced_path = Path(pack["replaced_path"]) if pack.get("replaced_path") else None
            replaced_backup = Path(pack["replaced_backup"]) if pack.get("replaced_backup") else None
            if replaced_path and replaced_backup:
                rollback_path(replaced_path, replaced_backup)
                ui_status("ok", f"Restored replaced pack: {replaced_path}")
        except Exception as e:
            ui_status("err", f"Rollback failed for pack {path}: {e}")
            log.warning("Rollback failed for pack %s: %s", path, e)

def find_installed_pack_path(server_dir: Path, pack_id: str, kind: str):
    """Find an existing installed pack folder by UUID and kind."""
    base = server_dir / ("resource_packs" if kind == "rp" else "behavior_packs")
    if not base.exists():
        return None
    for pack_dir in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not pack_dir.is_dir() or _is_builtin_pack(pack_dir.name):
            continue
        manifest_path = pack_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = load_json(manifest_path)
        except Exception:
            continue
        if manifest.get("header", {}).get("uuid") == pack_id:
            return pack_dir
    return None


def find_manifests(root):
    return sorted(root.rglob("manifest.json"))


def scan_addon_content(root):
    """Scan manifests and world markers once so large addons are not scanned repeatedly."""
    manifests = []
    worlds = set()
    markers = set(WORLD_MARKERS)
    log.info("Scan addon content: %s", root)
    for path in root.rglob("*"):
        if path.name == "manifest.json" and path.is_file():
            manifests.append(path)
        elif path.name in markers and path.is_file():
            worlds.add(path.parent)
    return sorted(manifests), sorted(worlds)


def find_world_dirs(root):
    worlds = set()
    for marker in WORLD_MARKERS:
        for file in root.rglob(marker):
            worlds.add(file.parent)
    return sorted(worlds)


def process_nested_archives(directory: Path, max_depth: int = 10) -> None:
    """Find and extract archive files inside a directory recursively."""
    found_any = True
    nested_count = 0
    failed: set[Path] = set()  # Track failed archives so they are not retried
    while found_any:
        if nested_count >= max_depth:
            log.warning("Nested archive depth limit reached (%d). Stop.", max_depth)
            break
        found_any = False
        log.info("Scan nested archives: %s", directory)
        for path in list(directory.rglob("*")):
            if path.is_file() and is_pack_file(path) and path not in failed:
                dest_dir = path.parent / f"_extracted_{path.stem}"
                log.info("Extracting nested archive: %s -> %s", path.name, dest_dir.name)
                try:
                    if is_tar_file(path):
                        safe_extract_tar(path, dest_dir)
                    else:
                        safe_extract(path, dest_dir)
                    path.unlink()
                    nested_count += 1
                    found_any = True
                    break  # Rescan after changing folder structure
                except Exception as e:
                    log.warning("Failed to extract nested archive %s: %s", path.name, e)
                    failed.add(path)
    if nested_count > 0:
        ui_status("ok", f"{plural(nested_count, 'sub-pack')} extracted.")


def temp_extract_dir(archive: Path) -> Path:
    """Local extract folder: .temp-addonInstaller/<addon-name>."""
    if DRY_RUN:
        raise RuntimeError("Dry-run must not create extraction directories.")
    temp_root = Path(__file__).resolve().parent / ".temp-addonInstaller"
    temp_root.mkdir(parents=True, exist_ok=True)
    name = clean_name(archive.name)
    dest = safe_child_path(temp_root, name, name)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def extract_archive_to_temp(archive: Path) -> Path:
    """Extract archive (zip or tar) to a temp directory and return its path."""
    if DRY_RUN:
        raise RuntimeError("Dry-run must not create extraction directories.")
    tmp = temp_extract_dir(archive)
    log.info("Prepare temp: %s", tmp)
    if is_tar_file(archive):
        log.info("Extract tar: %s", archive.name)
        safe_extract_tar(archive, tmp)
    else:
        log.info("Extract zip: %s", archive.name)
        safe_extract(archive, tmp)

    # Process nested archive files (.mcpack/.zip/etc.) inside
    process_nested_archives(tmp)
    return tmp


def list_archives(search_dirs) -> list:
    """Find all pack/archive files recursively in the given directories."""
    files = []
    seen_paths: set[Path] = set()
    for directory in search_dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            resolved = path.resolve()
            if path.is_file() and is_pack_file(path) and resolved not in seen_paths:
                files.append(resolved)
                seen_paths.add(resolved)
    return files


def choose_archive_location(server_dir):
    """Choose addon/template location through an interactive menu."""
    cwd = Path.cwd().resolve()

    while True:
        ui_menu("Addon source", [("Project", cwd)])
        ui_option("1", "Browse project folders", f"{cwd.name}/")
        ui_option("2", "Scan server folder", f"{server_dir.name}/")
        ui_option("3", "Enter custom folder or file")
        ui_option("0", "Exit")
        choice = ask("Select option", "1")

        if choice == "1":
            folders = visible_folders(cwd)
            if not folders:
                ui_status("warn", "No folders in this directory.")
                continue
            print(c_divider(f"Project folders: {cwd.name}/"))
            for idx, folder in enumerate(folders, 1):
                count = sum(1 for _ in folder.rglob("*") if _.is_file() and is_pack_file(_))
                count_str = c_green(f"({plural(count, 'file')})") if count > 0 else c_gray("(empty)")
                ui_option(str(idx), f"{folder.name}/", count_str)
            raw = ask("Select folder")
            try:
                index = int(raw)
            except (TypeError, ValueError):
                ui_status("warn", "Choice must be a number.")
                continue
            if index < 1 or index > len(folders):
                ui_status("warn", "Invalid folder choice.")
                continue
            picked = folders[index - 1]
            pick_count = sum(1 for _ in picked.rglob("*") if _.is_file() and is_pack_file(_))
            if pick_count == 0:
                ui_status("warn", f"No supported addon files in {picked.name}/. Pick another folder.")
                continue
            return picked

        if choice == "2":
            return server_dir.resolve()

        if choice == "3":
            manual = ask("Addon/template folder/file path", str(cwd))
            if not manual:
                continue
            path = Path(manual).expanduser().resolve()
            if not path.exists():
                ui_status("err", f"Path does not exist: {path}")
                continue
            return path

        ui_status("warn", "Choose 0, 1, 2, or 3.")


def get_key():
    """Read one key for the interactive picker."""
    if sys.platform == "win32":
        import msvcrt
        key = msvcrt.getch()
        if key == b"\x03":
            raise KeyboardInterrupt
        if key in (b"\x00", b"\xe0"):
            ext = msvcrt.getch()
            if ext == b"H":
                return "up"
            if ext == b"P":
                return "down"
            return ""
        if key in (b"\r", b"\n"):
            return "enter"
        if key == b" ":
            return "space"
        try:
            return key.decode("utf-8", errors="ignore").lower()
        except UnicodeDecodeError:
            return ""

    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x1b":
            seq = sys.stdin.read(2)
            if seq == "[A":
                return "up"
            if seq == "[B":
                return "down"
            return ""
        if ch in ("\r", "\n"):
            return "enter"
        if ch == " ":
            return "space"
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def archive_installed_hint(archive: Path, installed_keys) -> str:
    """Return picker hint when an archive contains packs already installed on server."""
    try:
        pack_keys = []
        for _, manifest in load_manifests_from_archive(archive):
            pack_id = manifest.get("header", {}).get("uuid")
            if not pack_id:
                continue
            for kind in detect_pack_kinds(manifest):
                pack_keys.append((pack_id, kind))
    except Exception as e:
        log.warning("Cannot inspect archive for installed hint %s: %s", archive, e)
        return ""

    if not pack_keys:
        return ""
    installed_count = sum(1 for key in pack_keys if key in installed_keys)
    if installed_count == len(pack_keys):
        return c_yellow("(already installed)")
    if installed_count:
        return c_yellow("(partially installed)")
    return ""

def build_archive_hints(candidates, server_dir):
    installed_keys = {
        (pack.get("pack_id"), pack.get("kind"))
        for pack in get_installed_addons(server_dir)
        if pack.get("pack_id") and pack.get("kind")
    }
    if not installed_keys:
        return {}
    return {
        path: hint
        for path in candidates
        if (hint := archive_installed_hint(path, installed_keys))
    }

def render_checkbox_picker(candidates, selected, cursor, search_dirs, archive_hints):
    """Render the interactive addon picker."""
    clear_screen()
    scan_names = ", ".join(d.name for d in search_dirs if d.exists())
    ui_menu("Select addons", [
        ("Found", plural(len(candidates), "file")),
        ("Selected", plural(len(selected), "file")),
        ("Source", scan_names or "-"),
    ])
    print()
    selected_order = {index: position + 1 for position, index in enumerate(selected)}
    for i, path in enumerate(candidates):
        hint = archive_hints.get(path, "")
        folder = f"({path.parent.name}/)"
        row_hint = f"{folder} {strip_ansi(hint)}".strip()
        ui_checkbox_row(i + 1, path.name, i in selected_order, i == cursor, row_hint, selected_order.get(i))
    print()
    ui_help("↑/↓ move", "Space select", "Enter continue")
    ui_help("a all", "c clear", "r refresh", "m add file", "q cancel")


def choose_archives_keyboard(candidates, search_dirs, server_dir):
    """Choose addons with arrow keys + Space, preserving the order they were selected."""
    if not candidates:
        return []
    cursor = 0
    selected = []
    archive_hints = build_archive_hints(candidates, server_dir)
    while True:
        render_checkbox_picker(candidates, selected, cursor, search_dirs, archive_hints)
        key = get_key()
        if key == "up":
            cursor = (cursor - 1) % len(candidates)
        elif key == "down":
            cursor = (cursor + 1) % len(candidates)
        elif key == "space":
            if cursor in selected:
                selected.remove(cursor)
            else:
                selected.append(cursor)
        elif key == "enter":
            print()
            return [candidates[i] for i in selected]
        elif key == "a":
            selected = list(range(len(candidates)))
        elif key == "c":
            selected.clear()
        elif key == "r":
            old_selected_paths = [candidates[i] for i in selected]
            candidates.clear()
            candidates.extend(list_archives(search_dirs))
            archive_hints.clear()
            archive_hints.update(build_archive_hints(candidates, server_dir))
            selected.clear()
            for old_path in old_selected_paths:
                if old_path in candidates:
                    selected.append(candidates.index(old_path))
            cursor = min(cursor, max(len(candidates) - 1, 0))
        elif key == "m":
            print()
            manual = ask("File path")
            if manual:
                manual_path = Path(manual).expanduser().resolve()
                if not manual_path.exists():
                    ui_status("err", f"File does not exist: {manual_path}")
                    ui_pause()
                    continue
                if not manual_path.is_file() or not is_pack_file(manual_path):
                    ui_status("err", f"File is not a Bedrock addon/template/archive: {manual_path}")
                    ui_pause()
                    continue
                candidates.append(manual_path)
                hint = archive_installed_hint(
                    manual_path,
                    {
                        (pack.get("pack_id"), pack.get("kind"))
                        for pack in get_installed_addons(server_dir)
                        if pack.get("pack_id") and pack.get("kind")
                    },
                )
                if hint:
                    archive_hints[manual_path] = hint
                selected.append(len(candidates) - 1)
                cursor = len(candidates) - 1
        elif key == "q":
            print()
            return []


def choose_archives_text(candidates, search_dirs, server_dir):
    """Text-input fallback addon picker, preserving the order toggles were selected."""
    selected = []
    archive_hints = build_archive_hints(candidates, server_dir)
    while True:
        ui_menu("Select addons", [
            ("Found", plural(len(candidates), "file")),
            ("Selected", plural(len(selected), "file")),
        ])
        selected_order = {index: position + 1 for position, index in enumerate(selected)}
        for i, path in enumerate(candidates, 1):
            hint = archive_hints.get(path, "")
            folder = f"({path.parent.name}/)"
            row_hint = f"{folder} {strip_ansi(hint)}".strip()
            ui_checkbox_row(i, path.name, i in selected_order, False, row_hint, selected_order.get(i))
        print()
        ui_kv("Input", "Toggle numbers, example: 1 or 1,3")
        ui_help("a all", "c clear", "r refresh", "0 add file", "q exit", "Enter continue")

        choice = ask("Choose addon/template", "", allow_exit=False)
        if is_exit_choice(choice) and choice != "0":
            raise UserExit
        if choice == "":
            break
        if choice.lower() == "a":
            selected = list(range(1, len(candidates) + 1))
            continue
        if choice.lower() == "c":
            selected.clear()
            continue
        if choice.lower() == "r":
            old_selected_paths = [candidates[i - 1] for i in selected]
            candidates.clear()
            candidates.extend(list_archives(search_dirs))
            archive_hints.clear()
            archive_hints.update(build_archive_hints(candidates, server_dir))
            selected.clear()
            for old_path in old_selected_paths:
                if old_path in candidates:
                    selected.append(candidates.index(old_path) + 1)
            ui_status("ok", f"Refreshed: {plural(len(candidates), 'file')} found.")
            continue
        if choice == "0":
            manual = ask("File path")
            if manual:
                manual_path = Path(manual).expanduser().resolve()
                if not manual_path.exists():
                    ui_status("err", f"File does not exist: {manual_path}")
                    continue
                if not manual_path.is_file() or not is_pack_file(manual_path):
                    ui_status("err", f"File is not a Bedrock addon/template/archive: {manual_path}")
                    continue
                candidates.append(manual_path)
                hint = archive_installed_hint(
                    manual_path,
                    {
                        (pack.get("pack_id"), pack.get("kind"))
                        for pack in get_installed_addons(server_dir)
                        if pack.get("pack_id") and pack.get("kind")
                    },
                )
                if hint:
                    archive_hints[manual_path] = hint
                selected.append(len(candidates))
            continue

        ok = True
        for part in [p.strip() for p in choice.split(",") if p.strip()]:
            if not part.isdigit():
                ok = False
                break
            index = int(part)
            if index < 1 or index > len(candidates):
                ok = False
                break
            if index in selected:
                selected.remove(index)
            else:
                selected.append(index)
        if not ok:
            ui_status("warn", "Invalid choice. Use numbers, example: 1 or 1,3.")

    return [candidates[i - 1] for i in selected]


def choose_archives(server_dir):
    while True:
        source = choose_archive_location(server_dir)
        if source.is_file():
            if not is_pack_file(source):
                ui_status("err", f"File is not a Bedrock addon/template/archive: {source}")
                continue
            return [source]

        search_dirs = [source]
        candidates = list_archives(search_dirs)
        if candidates:
            break
        ui_status("warn", f"No supported addon archives found in {source.name}/.")
        ui_status("info", "Pick another source.")

    if sys.stdin.isatty():
        return choose_archives_keyboard(candidates, search_dirs, server_dir)
    return choose_archives_text(candidates, search_dirs, server_dir)



def install_pack_dir(pack_dir, manifest, server_dir, source_name=None):
    kinds = detect_pack_kinds(manifest)
    if not kinds:
        log.info("Skip non-pack manifest %s: %s", pack_dir, manifest_kind_label(manifest))
        return []

    header = manifest.get("header", {})
    pack_id = header.get("uuid")
    # Validate UUID format before use
    validate_uuid(pack_id, context=str(pack_dir))
    version = version_array(header.get("version"))
    pack_name = title_for_pack(Path(pack_dir), manifest, source_name)
    log.info("Pack found: %s | kind=%s | uuid=%s | version=%s",
             pack_name, kinds, pack_id, version)

    installed = []
    for kind in kinds:
        base = server_dir / ("resource_packs" if kind == "rp" else "behavior_packs")
        if not DRY_RUN:
            base.mkdir(parents=True, exist_ok=True)
        else:
            action(f"Ensure dir: {base}")
        dest = base / pack_folder_name(pack_dir, manifest, kind, source_name)
        replaced_path = None
        replaced_backup = None
        existing_dest = find_installed_pack_path(server_dir, pack_id, kind)
        if existing_dest and existing_dest.resolve() != dest.resolve():
            if yes_no(f"Pack UUID already exists in {existing_dest.name}. Replace and rename folder?", True):
                replaced_path = existing_dest
                replaced_backup = backup_existing(existing_dest)
                log.info("Remove old pack folder after rename: %s", existing_dest)
                if not DRY_RUN:
                    shutil.rmtree(existing_dest)
            else:
                raise RuntimeError("Cancelled because the pack UUID is already installed.")
        record = {
            "pack_id": pack_id,
            "version": version,
            "path": str(dest),
            "backup": None,
            "replaced_path": str(replaced_path) if replaced_path else None,
            "replaced_backup": str(replaced_backup) if replaced_backup else None,
            "name": pack_name,
            "kind": kind,
            "dependencies": manifest_dependencies(manifest),
        }
        # Track the replacement before copying the new pack. If safe_copytree fails
        # after an old UUID-matching folder was moved/removed, rollback_install()
        # can still restore replaced_path from replaced_backup.
        installed.append(record)
        backup = safe_copytree(pack_dir, dest)
        record["backup"] = str(backup) if backup else None
    return installed


def template_world_default_name(src_world: Path) -> str:
    levelname_file = src_world / "levelname.txt"
    default_name = levelname_file.read_text(errors="ignore").strip() if levelname_file.exists() else src_world.name
    return default_name or src_world.name


def next_bedrock_world_name(worlds_dir: Path) -> str:
    """Suggest Bedrock level, Bedrock level-2, Bedrock level-3, etc."""
    base = "Bedrock level"
    existing = {folder.name.lower() for folder in visible_folders(worlds_dir)}
    if base.lower() not in existing:
        return base
    index = 2
    while True:
        candidate = f"{base}-{index}"
        if candidate.lower() not in existing:
            return candidate
        index += 1


def choose_world_to_replace(existing_worlds):
    print(c_divider("Replace existing world"))
    for i, world in enumerate(existing_worlds, 1):
        ui_option(str(i), world.name, str(world))
    ui_option("0", "Skip world import")
    while True:
        choice = ask("World to replace", "0", allow_exit=False)
        if is_exit_choice(choice):
            return None
        try:
            idx = int(choice)
        except ValueError:
            ui_status("warn", "Enter a valid number.")
            continue
        if 1 <= idx <= len(existing_worlds):
            return existing_worlds[idx - 1]
        ui_status("warn", f"Choose a number 0-{len(existing_worlds)}.")


def import_world_as_new(src_world: Path, worlds_dir: Path, default_name: str, server_dir: Path, source_name=None):
    suggested_name = next_bedrock_world_name(worlds_dir)
    if suggested_name != default_name:
        ui_kv("Suggested folder", suggested_name)
    while True:
        world_name = safe_world_name(ask(f"New world folder name for template {src_world.name}", suggested_name))
        dest = safe_child_path(worlds_dir, world_name, world_name)
        if dest.exists():
            ui_status("warn", f"World folder already exists: {world_name}. Choose another name or replace existing world.")
            continue
        break

    backup = safe_copytree(src_world, dest)
    renamed_local_packs = normalize_world_local_pack_folders(dest, source_name)
    record = {
        "path": dest,
        "backup": backup,
        "action": "created",
        "source": src_world.name,
        "renamed_local_packs": renamed_local_packs,
        "level_name_changed": False,
        "manual_level_name": False,
        "config_changes": [],
    }

    if yes_no(f'Set server.properties level-name to "{world_name}" so server uses this new world?', True):
        props, config_backup = set_server_level_name(server_dir, world_name)
        record["config_changes"].append((props, config_backup))
        record["level_name_changed"] = True
        ui_status("ok", f"server.properties level-name={world_name}")
    else:
        record["manual_level_name"] = True
        ui_status("info", "server.properties not changed. Set level-name manually if server should load this world.")
    return record


def import_world_replace(src_world: Path, existing_worlds, source_name=None):
    dest = choose_world_to_replace(existing_worlds)
    if dest is None:
        ui_status("warn", "Skipped world import.")
        return None

    ui_status("err", f"This replaces current world folder and progress: {dest.name}")
    ui_status("err", "A backup will be created first, but active world progress will be overwritten.")
    if not yes_no("Replace this world now?", False):
        ui_status("warn", "Skipped world replacement.")
        return None

    backup = replace_copytree(src_world, dest)
    renamed_local_packs = normalize_world_local_pack_folders(dest, source_name)
    return {
        "path": dest,
        "backup": backup,
        "action": "replaced",
        "source": src_world.name,
        "renamed_local_packs": renamed_local_packs,
        "level_name_changed": False,
        "manual_level_name": False,
        "config_changes": [],
    }


def import_world_dir(src_world, server_dir, source_name=None):
    worlds_dir = server_dir / "worlds"
    if not DRY_RUN:
        worlds_dir.mkdir(parents=True, exist_ok=True)
    else:
        action(f"Ensure dir: {worlds_dir}")

    src_world = Path(src_world)
    default_name = template_world_default_name(src_world)
    existing_worlds = visible_folders(worlds_dir)

    print(c_divider(f"World import: {src_world.name}"))
    ui_kv("Template name", default_name)
    ui_option("1", "Create new independent world", "does not touch existing worlds")
    if existing_worlds:
        ui_option("2", "Replace existing world", "backs up then overwrites world progress")
    ui_option("0", "Skip world import")

    while True:
        choice = ask("World import action", "1", allow_exit=False)
        if is_exit_choice(choice):
            ui_status("warn", "Skipped world import.")
            return None
        if choice == "1":
            return import_world_as_new(src_world, worlds_dir, default_name, server_dir, source_name)
        if choice == "2" and existing_worlds:
            return import_world_replace(src_world, existing_worlds, source_name)
        valid = "0, 1, or 2" if existing_worlds else "0 or 1"
        ui_status("warn", f"Choose {valid}.")


def is_pack_name(name: str) -> bool:
    lower_name = name.lower()
    return Path(lower_name).suffix in PACK_EXTS or any(lower_name.endswith(ext) for ext in TAR_EXTS)

def parse_manifest_bytes(source_label: str, data: bytes):
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        ui_status("warn", f"Skip invalid manifest: {source_label} ({e})")
        log.warning("Skip invalid manifest %s: %s", source_label, e)
        return None

def load_manifests_from_zip_file(z, source_label: str, depth: int, max_depth: int):
    manifests = []
    for name in z.namelist():
        if Path(name).name == "manifest.json":
            with z.open(name) as src:
                manifest = parse_manifest_bytes(f"{source_label}!{name}", src.read())
                if manifest is not None:
                    manifests.append((f"{source_label}!{name}", manifest))
            continue
        if depth >= max_depth or not is_pack_name(name):
            continue
        with z.open(name) as src:
            nested_data = src.read()
        manifests.extend(load_manifests_from_archive_bytes(nested_data, f"{source_label}!{name}", depth + 1, max_depth))
    return manifests

def load_manifests_from_tar_file(tf, source_label: str, depth: int, max_depth: int):
    manifests = []
    for member in tf.getmembers():
        if not member.isfile():
            continue
        src = tf.extractfile(member)
        if src is None:
            continue
        with src:
            data = src.read()
        if Path(member.name).name == "manifest.json":
            manifest = parse_manifest_bytes(f"{source_label}!{member.name}", data)
            if manifest is not None:
                manifests.append((f"{source_label}!{member.name}", manifest))
            continue
        if depth >= max_depth or not is_pack_name(member.name):
            continue
        manifests.extend(load_manifests_from_archive_bytes(data, f"{source_label}!{member.name}", depth + 1, max_depth))
    return manifests

def load_manifests_from_archive_bytes(data: bytes, source_label: str, depth: int, max_depth: int):
    if depth > max_depth:
        ui_status("warn", f"Dry-run nested archive depth limit reached: {source_label}")
        return []
    bio = io.BytesIO(data)
    if zipfile.is_zipfile(bio):
        bio.seek(0)
        with zipfile.ZipFile(bio) as z:
            return load_manifests_from_zip_file(z, source_label, depth, max_depth)
    bio.seek(0)
    try:
        with tarfile.open(fileobj=bio, mode="r:*") as tf:
            return load_manifests_from_tar_file(tf, source_label, depth, max_depth)
    except tarfile.TarError:
        ui_status("warn", f"Dry-run skipped unsupported nested archive: {source_label}")
        return []

def load_manifests_from_archive(archive: Path, max_depth: int = 10):
    """Read manifest.json from an archive and nested pack archives without full install extraction."""
    manifests = []
    if is_tar_file(archive):
        with tarfile.open(archive) as tf:
            manifests.extend(load_manifests_from_tar_file(tf, archive.name, 0, max_depth))
    else:
        with zipfile.ZipFile(archive) as z:
            manifests.extend(load_manifests_from_zip_file(z, archive.name, 0, max_depth))
    return manifests


def dry_run_install_manifest(manifest_name: str, manifest: dict, server_dir: Path, source_name=None):
    """Simulate pack installation from manifest without full archive extraction."""
    installed = []
    header = manifest.get("header", {})
    pack_id = header.get("uuid")
    version = version_array(header.get("version"))
    validate_uuid(pack_id, f"manifest {manifest_name} header.uuid")
    kinds = detect_pack_kinds(manifest)
    if not kinds:
        log.info("Skip non-pack manifest %s: %s", manifest_name, manifest_kind_label(manifest))
        return installed

    for kind in kinds:
        base = server_dir / ("resource_packs" if kind == "rp" else "behavior_packs")
        log.info("Dry-run ensure dir: %s", base)
        virtual_pack_dir = Path(manifest_name).parent
        if str(virtual_pack_dir) in ("", "."):
            virtual_pack_dir = Path(archive_stem_from_manifest(manifest_name))
        dest = base / pack_folder_name(virtual_pack_dir, manifest, kind, source_name)
        log.info("Dry-run would install: %s -> %s", manifest_name, dest)
        installed.append({
            "pack_id": pack_id,
            "version": version,
            "path": str(dest),
            "backup": None,
            "name": title_for_pack(virtual_pack_dir, manifest, source_name),
            "kind": kind,
            "dependencies": manifest_dependencies(manifest),
        })
    return installed


def format_version(version) -> str:
    if isinstance(version, list):
        return ".".join(map(str, version))
    if version is None:
        return "unknown"
    return str(version)


def kind_name(kind: str) -> str:
    return "Resource Pack" if kind == "rp" else "Behavior Pack"


def inspect_version(manifest: dict) -> str:
    """Return a readable version string without failing the whole inspect report."""
    raw_version = manifest.get("header", {}).get("version")
    try:
        return format_version(version_array(raw_version))
    except Exception:
        return f"{format_version(raw_version)} (invalid)"


def inspect_archive(archive: Path) -> None:
    """Print addon archive metadata without extracting or installing."""
    if not is_pack_file(archive):
        raise RuntimeError(f"File is not a supported addon/template/archive: {archive}")

    validate_archive(archive)
    size_mb = archive.stat().st_size / (1024 * 1024)
    manifest_items = load_manifests_from_archive(archive)
    bp_count, rp_count = pack_kind_counts(manifest_items)

    print(c_divider("Inspect addon"))
    ui_kv("File", archive)
    ui_kv("Size", f"{size_mb:.1f} MB")
    ui_kv("Manifests", plural(len(manifest_items), "manifest"))
    ui_kv("Detected", f"{bp_count} BP, {rp_count} RP")

    if not manifest_items:
        ui_status("warn", "No manifest.json files found in this archive.")
        return

    for index, (manifest_name, manifest) in enumerate(manifest_items, 1):
        header = manifest.get("header", {})
        pack_name = header.get("name") or "Unnamed pack"
        pack_id = header.get("uuid") or "missing"
        kind_labels = manifest_kind_label(manifest)
        dependencies = manifest_dependencies(manifest)

        print(c_divider(f"Manifest {index}: {pack_name}"))
        ui_kv("Source", manifest_name)
        ui_kv("Name", pack_name)
        ui_kv("Kind", kind_labels)
        ui_kv("UUID", pack_id)
        ui_kv("Version", inspect_version(manifest))

        try:
            validate_uuid(pack_id, context=str(manifest_name))
        except RuntimeError as e:
            ui_status("warn", str(e))

        if dependencies:
            ui_kv("Dependencies", plural(len(dependencies), "dependency", "dependencies"))
            for dep in dependencies:
                ui_kv("  UUID", dep["uuid"])
                ui_kv("  Version", format_version(dep.get("version")))
        else:
            ui_kv("Dependencies", "none")


def build_archive_batch_context(archives, server_dir):
    """Inspect selected archives so Step 4 can explain split BP/RP installs."""
    archive_items = {}
    selected_ids = set()
    server_ids = {
        pack.get("pack_id")
        for pack in get_installed_addons(server_dir)
        if pack.get("pack_id")
    }
    bp_count = 0
    rp_count = 0
    dependencies = []

    for archive in archives:
        archive_path = Path(archive).expanduser().resolve()
        try:
            items = load_manifests_from_archive(archive_path)
        except Exception as e:
            log.warning("Cannot pre-scan archive %s: %s", archive_path, e)
            archive_items[archive_path] = []
            continue
        archive_items[archive_path] = items
        for _, manifest in items:
            header = manifest.get("header", {})
            pack_id = header.get("uuid")
            kinds = detect_pack_kinds(manifest)
            if pack_id and kinds:
                selected_ids.add(pack_id)
            if "bp" in kinds:
                bp_count += 1
            if "rp" in kinds:
                rp_count += 1
            dependencies.extend(manifest_dependencies(manifest))

    return {
        "archive_items": archive_items,
        "available_ids": selected_ids | server_ids,
        "selected_ids": selected_ids,
        "server_ids": server_ids,
        "bp_count": bp_count,
        "rp_count": rp_count,
        "dependencies": dependencies,
    }


def installed_pack_index(server_dir):
    """Build lookups for installed packs by UUID/kind and folder name."""
    by_key = {}
    by_folder = {}
    for pack in get_installed_addons(server_dir):
        pack_id = pack.get("pack_id")
        kind = pack.get("kind")
        path = pack.get("path")
        if pack_id and kind:
            by_key[(pack_id, kind)] = pack
        if path:
            by_folder[Path(path).name.lower()] = pack
    return {"by_key": by_key, "by_folder": by_folder}


def _virtual_pack_dir(archive: Path, manifest_name: str) -> Path:
    label = str(manifest_name).split("!", 1)[-1]
    parent = Path(label).parent
    if str(parent) in ("", "."):
        return Path(archive.stem)
    return parent


def manifest_pack_records(archive: Path, manifest_items, server_dir: Path):
    """Convert pre-scanned manifest items into per-kind install records."""
    source_name = archive.name
    records = []
    for manifest_name, manifest in manifest_items:
        header = manifest.get("header", {})
        pack_id = header.get("uuid")
        kinds = detect_pack_kinds(manifest)
        if not pack_id or not kinds:
            continue
        try:
            version = version_array(header.get("version"))
        except Exception:
            version = header.get("version")
        pack_dir = _virtual_pack_dir(archive, str(manifest_name))
        pack_name = title_for_pack(pack_dir, manifest, source_name)
        for kind in kinds:
            base = server_dir / ("resource_packs" if kind == "rp" else "behavior_packs")
            dest = base / pack_folder_name(pack_dir, manifest, kind, source_name)
            records.append({
                "archive": archive,
                "manifest_name": str(manifest_name),
                "name": pack_name,
                "pack_id": pack_id,
                "version": version,
                "kind": kind,
                "dest": dest,
            })
    return records


def scan_install_conflicts(archives, server_dir, batch_context):
    """Detect install conflicts before extraction/copy starts."""
    index = installed_pack_index(server_dir)
    conflicts = []
    seen = {}
    archive_items = batch_context.get("archive_items", {})

    for archive in archives:
        archive_path = Path(archive).expanduser().resolve()
        records = manifest_pack_records(archive_path, archive_items.get(archive_path, []), server_dir)
        for record in records:
            key = (record["pack_id"], record["kind"])
            installed_pack = index["by_key"].get(key)
            if installed_pack:
                conflicts.append({
                    "type": "installed_uuid",
                    "record": record,
                    "installed": installed_pack,
                })
                if installed_pack.get("version") != record.get("version"):
                    conflicts.append({
                        "type": "version_change",
                        "record": record,
                        "installed": installed_pack,
                    })

            previous = seen.get(key)
            if previous:
                conflicts.append({
                    "type": "batch_duplicate_uuid",
                    "record": record,
                    "previous": previous,
                })
            else:
                seen[key] = record

            dest = record["dest"]
            if dest.exists():
                conflicts.append({
                    "type": "dest_exists",
                    "record": record,
                    "installed": index["by_folder"].get(dest.name.lower()),
                })
    return conflicts


def print_install_conflicts(conflicts) -> None:
    if not conflicts:
        ui_status("ok", "No install conflicts found.")
        return

    ui_panel("Install conflicts", [
        f"{ui_badge('WARN', 'warn')} Found {plural(len(conflicts), 'possible conflict')} before copying files.",
        ("Safety", "Installer will still ask before replacing files."),
    ], kind="warn")
    for idx, conflict in enumerate(conflicts, 1):
        record = conflict["record"]
        installed = conflict.get("installed") or {}
        kind = "RP" if record["kind"] == "rp" else "BP"
        print(f"\n  {idx}. {c_bold(record['name'])} {c_gray(f'[{kind}]')}")
        ui_kv("  Type", conflict["type"].replace("_", " "))
        ui_kv("  UUID", record["pack_id"])
        ui_kv("  Archive", record["archive"].name)
        ui_kv("  Selected", f"v{format_version(record.get('version'))} -> {short_path(record['dest'])}")
        if installed:
            ui_kv("  Installed", f"v{format_version(installed.get('version'))} -> {short_path(installed.get('path'))}")
        previous = conflict.get("previous")
        if previous:
            ui_kv("  Previous", f"{previous['archive'].name} -> {short_path(previous['dest'])}")


def confirm_install_conflicts(conflicts) -> bool:
    """Return True to continue after conflict report, False to cancel."""
    if not conflicts:
        return True
    print()
    ui_option("1", "Continue", "keep safety prompts before replacing files")
    ui_option("0", "Cancel install")
    while True:
        choice = ask("Conflict action", "0", allow_exit=False)
        if is_exit_choice(choice):
            return False
        if choice == "1":
            return True
        ui_status("warn", "Choose 1 to continue or 0 to cancel.")


def print_archive_batch_overview(context, archive_count) -> None:
    """Print a compact overview before individual archive processing."""
    rows = [
        ("Archives", plural(archive_count, "archive")),
        ("Detected", f"{context['bp_count']} BP, {context['rp_count']} RP"),
    ]
    deps = context["dependencies"]
    if deps:
        matched = sum(1 for dep in deps if dep["uuid"] in context["available_ids"])
        rows.append(("Dependencies", f"{matched}/{len(deps)} manifest dependencies found"))
    ui_panel("Selected content", rows, kind="info")
    if context["bp_count"] and context["rp_count"]:
        ui_status("ok", "BP/RP packs are present across this install batch.")
    elif context["bp_count"]:
        ui_status("warn", "Only Behavior Packs detected in the selected archives.")
    elif context["rp_count"]:
        ui_status("warn", "Only Resource Packs detected in the selected archives.")
    if deps and matched != len(deps):
        ui_status("warn", f"Manifest dependencies found: {matched}/{len(deps)}.")


def pack_kind_counts(pack_items) -> tuple[int, int]:
    """Return behavior/resource pack counts for manifest items."""
    bp_count = sum(1 for _, manifest in pack_items if "bp" in detect_pack_kinds(manifest))
    rp_count = sum(1 for _, manifest in pack_items if "rp" in detect_pack_kinds(manifest))
    return bp_count, rp_count


def world_template_count(pack_items) -> int:
    """Return the number of world template manifests in scanned content."""
    return sum(1 for _, manifest in pack_items if "world_template" in manifest_module_types(manifest))


def format_detected_content(bp_count: int, rp_count: int, template_count: int, world_count: int) -> str:
    parts = [f"{bp_count} BP", f"{rp_count} RP", plural(world_count, "world")]
    return ", ".join(parts)


def pack_content_label(bp_count: int, rp_count: int) -> str:
    """Return a compact human label for detected pack content."""
    if bp_count and rp_count:
        return f"BP + RP ({bp_count} BP, {rp_count} RP)"
    if bp_count:
        return f"BP only ({bp_count})"
    if rp_count:
        return f"RP only ({rp_count})"
    return "no BP/RP packs"


def print_pack_kind_notice(pack_items, batch_available_ids=None) -> None:
    """Print only extra scan notes that are not already covered by the detected row."""
    batch_available_ids = batch_available_ids or set()
    bp_count, rp_count = pack_kind_counts(pack_items)
    dependencies = [
        dep
        for _, manifest in pack_items
        for dep in manifest_dependencies(manifest)
    ]
    matched_deps = [dep for dep in dependencies if dep["uuid"] in batch_available_ids]
    if bp_count and not rp_count and not matched_deps:
        ui_subitem(c_warn("Note:"), "Install companion resource pack separately if needed.")


def process_archive(archive, server_dir, batch_context=None):
    archive = Path(archive).expanduser().resolve()
    batch_context = batch_context or {}
    batch_available_ids = batch_context.get("available_ids", set())
    validate_archive(archive)
    size_mb = archive.stat().st_size / (1024 * 1024)

    if DRY_RUN:
        ui_phase("Dry-run scan", "reads manifests without extraction")
        installed = []
        manifest_items = load_manifests_from_archive(archive)
        bp_count, rp_count = pack_kind_counts(manifest_items)
        template_count = world_template_count(manifest_items)
        ui_subitem(c_gray("Detected:"), format_detected_content(bp_count, rp_count, template_count, 0))
        print_pack_kind_notice(manifest_items, batch_available_ids)
        ui_phase("Simulate install")
        for manifest_name, manifest in manifest_items:
            try:
                result = dry_run_install_manifest(manifest_name, manifest, server_dir, archive.name)
                for pack in result:
                    kind_label = "RP" if pack["kind"] == "rp" else "BP"
                    ui_subitem(c_ok(f"{kind_label}"), f"{pack['name']} {c_gray('-> ' + short_path(pack['path']))}")
                installed.extend(result)
            except Exception as e:
                ui_status("warn", f"Skip invalid manifest: {manifest_name} ({e})")
                log.warning("Skip invalid manifest %s in %s: %s", manifest_name, archive, e)
        return installed, []

    ui_phase("Extract archive", f"{size_mb:.1f} MB")
    tmp = extract_archive_to_temp(archive)
    installed = []
    imported_worlds = []
    try:
        ui_phase("Scan content")
        manifests, world_dirs = scan_addon_content(tmp)
        manifest_items = []
        for manifest_path in manifests:
            try:
                manifest_items.append((manifest_path, load_json(manifest_path)))
            except Exception as e:
                ui_status("warn", f"Skip invalid manifest: {manifest_path} ({e})")
                log.warning("Skip invalid manifest %s: %s", manifest_path, e)
        bp_count, rp_count = pack_kind_counts(manifest_items)
        template_count = world_template_count(manifest_items)
        ui_subitem(c_gray("Detected:"), format_detected_content(bp_count, rp_count, template_count, len(world_dirs)))
        print_pack_kind_notice(manifest_items, batch_available_ids)

        ui_phase("Install packs")
        for manifest_path, manifest in manifest_items:
            try:
                result = install_pack_dir(manifest_path.parent, manifest, server_dir, archive.name)
                for pack in result:
                    kind_label = "RP" if pack["kind"] == "rp" else "BP"
                    folder = Path(pack["path"]).parent.name + "/" + Path(pack["path"]).name
                    version = ".".join(map(str, pack["version"]))
                    ui_subitem(c_ok(f"{kind_label}"), f"{pack['name']} {c_gray('v' + version)}")
                    ui_subitem(c_gray("   ->"), folder)
                installed.extend(result)
            except Exception as e:
                ui_status("warn", f"Skip invalid manifest: {manifest_path} ({e})")
                log.warning("Skip invalid manifest %s: %s", manifest_path, e)

        if world_dirs:
            ui_phase("World imports")
        for world_dir in world_dirs:
            if yes_no(f"Template/world detected: {world_dir.name}. Import world?", True):
                imported = import_world_dir(world_dir, server_dir, archive.name)
                if imported:
                    imported_worlds.append(imported)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return installed, imported_worlds


def read_pack_list(path):
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        backup_existing(path)
        ui_status("warn", f"Broken JSON: {path}. It will be rewritten as an empty list plus new packs.")
        return []


def enable_pack(world_dir, installed):
    file_name = "world_resource_packs.json" if installed["kind"] == "rp" else "world_behavior_packs.json"
    path = world_dir / file_name
    packs = read_pack_list(path)
    packs = [p for p in packs if p.get("pack_id") != installed["pack_id"]]
    packs.append({"pack_id": installed["pack_id"], "version": installed["version"]})
    backup = write_text(path, json.dumps(packs, indent=2) + "\n")
    return path, backup


def check_texturepack_required(server_dir):
    props = server_dir / "server.properties"
    if not props.exists():
        return False
    content = props.read_text(encoding="utf-8", errors="ignore")
    for line in content.splitlines():
        if line.strip().startswith("texturepack-required="):
            parts = line.split("=", 1)
            if len(parts) > 1:
                return parts[1].strip().lower() == "true"
    return False


def set_texturepack_required(server_dir):
    props = server_dir / "server.properties"
    content = props.read_text(encoding="utf-8", errors="ignore")
    lines = content.splitlines()
    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith("texturepack-required="):
            new_lines.append("texturepack-required=true")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append("texturepack-required=true")
    # Use consistent newlines (\n) for cross-platform compatibility
    return write_text(props, "\n".join(new_lines) + "\n")


def imported_world_path(world):
    return world["path"] if isinstance(world, dict) else world

def choose_world(server_dir, imported_worlds):
    if imported_worlds:
        print(c_divider("Imported worlds"))
        for i, w in enumerate(imported_worlds, 1):
            path = imported_world_path(w)
            ui_option(str(i), path.name, str(path))
        if yes_no("Use this imported world to enable packs?", True):
            if len(imported_worlds) == 1:
                return imported_world_path(imported_worlds[0])
            # Handle non-integer input without crashing
            while True:
                raw = ask("Choose imported world", "1")
                try:
                    idx = int(raw)
                    if 1 <= idx <= len(imported_worlds):
                        return imported_world_path(imported_worlds[idx - 1])
                    ui_status("warn", f"Choose a number 1-{len(imported_worlds)}.")
                except ValueError:
                    ui_status("warn", "Enter a valid number.")

    worlds_dir = server_dir / "worlds"
    if not DRY_RUN:
        worlds_dir.mkdir(parents=True, exist_ok=True)
    else:
        action(f"Ensure dir: {worlds_dir}")
    existing = visible_folders(worlds_dir)

    if existing:
        print(c_divider("World folders"))
        for i, w in enumerate(existing, 1):
            ui_option(str(i), w.name)
        ui_option("0", "Create/use another name")
        ui_help("q/exit cancel")
        # Handle non-integer input without crashing
        while True:
            choice = ask("Choose world", "1", allow_exit=False)
            if is_exit_choice(choice) and choice != "0":
                raise UserExit
            try:
                idx = int(choice)
                if idx == 0:
                    break
                if 1 <= idx <= len(existing):
                    return existing[idx - 1]
                ui_status("warn", f"Choose a number 0-{len(existing)}.")
            except ValueError:
                ui_status("warn", "Enter a valid number.")

    prop_name = read_server_level_name(server_dir)
    default_name = prop_name or "Bedrock level"

    ui_status("warn", "No world folder exists in this server yet.")
    ui_kv("Default", default_name)

    if yes_no(f"Create world named \"{default_name}\"?", True):
        world_name = safe_world_name(default_name)
    else:
        world_name = safe_world_name(ask("Enter world name", default_name))

    world_dir = safe_child_path(worlds_dir, world_name, world_name)
    if not DRY_RUN:
        world_dir.mkdir(parents=True, exist_ok=True)
    else:
        action(f"Ensure dir: {world_dir}")
    return world_dir


def choose_existing_world(server_dir):
    worlds_dir = server_dir / "worlds"
    existing = visible_folders(worlds_dir)
    if not existing:
        ui_status("warn", "No world folders found.")
        return None
    print(c_divider("Choose world"))
    for i, world in enumerate(existing, 1):
        ui_option(str(i), world.name)
    ui_option("0", "Exit")
    while True:
        raw = ask("Choose world", "1")
        try:
            idx = int(raw)
            if 1 <= idx <= len(existing):
                return existing[idx - 1]
            ui_status("warn", f"Choose a number 1-{len(existing)}.")
        except ValueError:
            ui_status("warn", "Enter a valid number.")

def _tick(cond: bool) -> str:
    """Check/cross symbol with color when available."""
    if _COLOR_ENABLED:
        return c_green("[\u2713]") if cond else c_gray("[ ]")
    return "[✓]" if cond else "[ ]"


def ui_pack_presence(label: str, included: bool) -> None:
    """Print pack presence with a green label when included."""
    label_text = c_green(label) if included else c_gray(label)
    value = _tick(True) if included else c_gray("not included")
    print(f"  {label_text}: {value}")


def print_summary(archive_results, dep_missing) -> None:
    """Print a separate summary for each addon/archive."""
    for archive_name, packs, worlds in archive_results:
        rows = [
            ("Installed", plural(len(packs), "pack")),
            ("Imported worlds", plural(len(worlds), "world")),
        ]
        if DRY_RUN:
            rows.append(("Mode", ui_badge("DRY-RUN", "warn")))
        ui_panel(f"Summary: {archive_name}", rows, kind="ok")

        # Show processed addon name and UUID
        seen = set()
        for p in packs:
            pid = p["pack_id"]
            if pid not in seen:
                seen.add(pid)
                ui_status("ok", f"{p['name']} {c_gray('(' + pid + ')')}")

        # BP Included
        bp_packs = [x for x in packs if x["kind"] == "bp"]
        has_bp = len(bp_packs) > 0
        ui_pack_presence("Behavior Pack", has_bp)
        if has_bp:
            for p in bp_packs:
                ui_kv("  Location", p["path"])
                ui_kv("  Version", ".".join(map(str, p["version"])))

        # RP Included
        rp_packs = [x for x in packs if x["kind"] == "rp"]
        has_rp = len(rp_packs) > 0
        ui_pack_presence("Resource Pack", has_rp)
        if has_rp:
            for p in rp_packs:
                ui_kv("  Location", p["path"])
                ui_kv("  Version", ".".join(map(str, p["version"])))

        # World Import
        for w in worlds:
            path = imported_world_path(w)
            action_label = "replaced existing world" if w.get("action") == "replaced" else "created new world"
            ui_kv("  World", path)
            ui_kv("  Source", w.get("source", "unknown"))
            ui_kv("  Action", action_label)
            if w.get("backup"):
                ui_kv("  Backup", w["backup"])
            renamed_local_packs = w.get("renamed_local_packs") or []
            if renamed_local_packs:
                ui_kv("  Local packs renamed", plural(len(renamed_local_packs), "folder"))
                for renamed in renamed_local_packs:
                    ui_kv("    Folder", f"{renamed['old']} -> {renamed['new']}")
            if w.get("level_name_changed"):
                ui_kv("  server.properties", "level-name updated")
            elif w.get("manual_level_name"):
                ui_kv("  server.properties", "manual level-name update needed")

        # Missing Deps per addon
        pack_ids = {p["pack_id"] for p in packs}
        local_missing = [(p, d) for p, d in dep_missing if p["pack_id"] in pack_ids]
        if local_missing:
            ui_status("err", f"Action needed: {plural(len(local_missing), 'missing manifest dependency', 'missing manifest dependencies')}.")
            ui_kv("  Note", "Dependency type is unknown; it may be BP, RP, or another pack.")
            for pack, dep in local_missing:
                ui_kv("  Pack", c_err(pack["name"]))
                ui_kv("  Needs", f"{c_yellow(dep['uuid'])} version {dep.get('version')} (type unknown)")
        else:
            ui_status("ok", "No missing dependencies.")


# Built-in Minecraft folder prefixes/patterns that must not be deleted
_BUILTIN_PREFIXES = (
    "vanilla", "chemistry", "editor", "experimental_",
    "server_editor_library", "server_library", "server_ui_library",
)


def _is_builtin_pack(folder_name: str) -> bool:
    """Check whether this folder is a built-in Minecraft server pack."""
    name_lower = folder_name.lower()
    # Skip folder backup (.bak-)
    if ".bak-" in name_lower:
        return True
    return any(name_lower == prefix or name_lower.startswith(prefix)
               for prefix in _BUILTIN_PREFIXES)


def get_installed_addons(server_dir):
    """Scan only user-installed addons, skipping Minecraft built-ins and backups."""
    installed = []
    for kind, folder_name in [("rp", "resource_packs"), ("bp", "behavior_packs")]:
        base = server_dir / folder_name
        if not base.exists():
            continue
        for pack_dir in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not pack_dir.is_dir():
                continue
            if _is_builtin_pack(pack_dir.name):
                log.debug("Skip built-in/backup: %s", pack_dir.name)
                continue
            manifest_path = pack_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = load_json(manifest_path)
                header = manifest.get("header", {})
                installed.append({
                    "path": pack_dir,
                    "name": manifest_display_name(pack_dir, manifest),
                    "pack_id": header.get("uuid"),
                    "version": version_array(header.get("version")),
                    "kind": kind
                })
            except Exception as e:
                log.warning("Failed to read %s: %s", manifest_path, e)
    return installed

def world_pack_order_maps(world_dir):
    """Return pack_id order maps for a world's BP/RP JSON files."""
    order_maps = {}
    for kind, file_name in (("rp", "world_resource_packs.json"), ("bp", "world_behavior_packs.json")):
        entries = read_pack_list(world_dir / file_name)
        order_maps[kind] = {
            entry.get("pack_id"): index
            for index, entry in enumerate(entries)
            if entry.get("pack_id")
        }
    return order_maps

def find_world_local_pack_path(world_dir: Path, pack_id: str, kind: str):
    """Find a pack bundled inside worlds/<world>/behavior_packs or resource_packs."""
    folder_name = "resource_packs" if kind == "rp" else "behavior_packs"
    base = world_dir / folder_name
    if not pack_id or not base.exists():
        return None
    for pack_dir in sorted([p for p in base.iterdir() if p.is_dir()], key=lambda p: p.name.lower()):
        manifest_path = pack_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = load_json(manifest_path)
        except Exception:
            continue
        if manifest.get("header", {}).get("uuid") == pack_id:
            return pack_dir
    return None


def pack_source_label(world_dir: Path, pack_id: str, kind: str) -> str:
    return "from imported world" if find_world_local_pack_path(world_dir, pack_id, kind) else ""


def source_suffix_for_items(items) -> str:
    sources = {
        item.get("source")
        for item in items.values()
        if item.get("source")
    }
    if not sources:
        return ""
    return f" {c_gray('(' + ', '.join(sorted(sources)) + ')')}"


def sort_addons_for_uninstall(candidates, world_dir=None):
    """Sort uninstall candidates by world pack-stack order when a world is selected."""
    if not world_dir:
        return candidates
    order_maps = world_pack_order_maps(world_dir)
    kind_order = {"rp": 0, "bp": 1}
    return sorted(
        candidates,
        key=lambda pack: (
            kind_order.get(pack["kind"], 9),
            order_maps.get(pack["kind"], {}).get(pack.get("pack_id"), 999999),
            pack["name"].lower(),
        ),
    )

def default_world_dir(server_dir):
    worlds_dir = server_dir / "worlds"
    prop_name = read_server_level_name(server_dir)
    if prop_name and (worlds_dir / prop_name).is_dir():
        return worlds_dir / prop_name
    existing = visible_folders(worlds_dir)
    return existing[0] if len(existing) == 1 else None

def installed_pack_lookup(server_dir):
    return {
        (pack.get("pack_id"), pack.get("kind")): pack
        for pack in get_installed_addons(server_dir)
        if pack.get("pack_id") and pack.get("kind")
    }


def addon_display_name(name: str) -> str:
    """Return a clean addon name for combined BP/RP overview rows."""
    return clean_pack_title(name)


def version_sort_value(version):
    return ".".join(map(str, version)) if isinstance(version, list) else str(version or "")


def format_group_versions(group):
    versions = {
        kind: version_sort_value(item.get("version"))
        for kind, item in group["items"].items()
        if item.get("version") is not None
    }
    if not versions:
        return ""
    if len(set(versions.values())) == 1:
        return f" {c_gray('v' + next(iter(versions.values())))}"
    parts = []
    if "bp" in versions:
        parts.append(f"BP v{versions['bp']}")
    if "rp" in versions:
        parts.append(f"RP v{versions['rp']}")
    return f" {c_gray(' / '.join(parts))}"


def world_ordered_addon_rows(server_dir, world_dir):
    """Return enabled world addons grouped into combined BP/RP rows."""
    pack_lookup = installed_pack_lookup(server_dir)
    groups = {}
    for kind, file_name, kind_order in (
        ("bp", "world_behavior_packs.json", 0),
        ("rp", "world_resource_packs.json", 1),
    ):
        entries = read_pack_list(world_dir / file_name)
        for index, entry in enumerate(entries, 1):
            pack_id = entry.get("pack_id", "")
            installed = pack_lookup.get((pack_id, kind)) or {}
            name = installed.get("name") or f"Unknown pack {pack_id[:8]}"
            version = entry.get("version")
            key = addon_group_key_from_name(name, pack_id or f"{kind}:{index}")
            group = groups.setdefault(key, {
                "names": [],
                "items": {},
                "orders": [],
            })
            group["names"].append(addon_display_name(name))
            group["items"][kind] = {
                "pack_id": pack_id,
                "name": name,
                "version": version,
                "source": pack_source_label(world_dir, pack_id, kind),
            }
            group["orders"].append((index, kind_order))

    rows = []
    for group in groups.values():
        rows.append({
            "name": min(group["names"], key=lambda value: (len(value), value.lower())),
            "items": group["items"],
            "order": min(group["orders"]),
        })
    return sorted(rows, key=lambda row: (row["order"], row["name"].lower()))


def addon_kind_label(items):
    labels = []
    if "bp" in items:
        labels.append(ui_badge("BP", "accent"))
    if "rp" in items:
        labels.append(ui_badge("RP", "info"))
    return " ".join(labels)


def print_installed_addon_overview(server_dir, world_dir=None):
    """Show enabled addon order before the action menu."""
    installed = get_installed_addons(server_dir)
    print()
    if not installed:
        ui_panel("Current addon status", [
            f"{ui_badge('WARN', 'warn')} No installed addons found in behavior_packs/resource_packs.",
            ("Next", "Choose Install addon to add packs to a world."),
        ], kind="warn")
        return

    if world_dir:
        rows = world_ordered_addon_rows(server_dir, world_dir)
        if rows:
            ui_panel("Current addon status", [
                ("World", world_dir.name),
                ("Showing", "addons enabled in this world, saved load order"),
                ("Tip", "Number 1 is first entry in world pack JSON"),
            ], kind="ok")
            for index, row in enumerate(rows, 1):
                kind_text = addon_kind_label(row["items"])
                print(f"  {ui_badge(str(index), 'muted')} {kind_text} {row['name']}{format_group_versions(row)}{source_suffix_for_items(row['items'])}")
            return
        ui_panel("Current addon status", [
            ("World", world_dir.name),
            f"{ui_badge('WARN', 'warn')} No enabled pack entries found in this world.",
            ("Next", "Choose Install addon to enable packs."),
        ], kind="warn")

    grouped = {}
    for pack in installed:
        key = addon_group_key_from_name(pack["name"], pack.get("pack_id") or pack["name"])
        item = grouped.setdefault(key, {"names": [], "items": {}})
        item["names"].append(addon_display_name(pack["name"]))
        item["items"][pack["kind"]] = pack

    ui_panel("Current addon status", [
        ("World", "not selected"),
        ("Showing", "installed folders only; not world load order"),
    ])
    for idx, data in enumerate(sorted(grouped.values(), key=lambda item: min(item["names"]).lower()), 1):
        name = min(data["names"], key=lambda value: (len(value), value.lower()))
        kind_text = addon_kind_label(data["items"])
        print(f"  {ui_badge(str(idx), 'muted')} {kind_text} {name}{format_group_versions(data)}")

def installed_pack_name_map(server_dir):
    return {
        (pack.get("pack_id"), pack.get("kind")): pack.get("name")
        for pack in get_installed_addons(server_dir)
        if pack.get("pack_id") and pack.get("kind")
    }

def world_pack_paths(world_dir):
    return {
        "bp": world_dir / "world_behavior_packs.json",
        "rp": world_dir / "world_resource_packs.json",
    }


def combined_world_reorder_entries(server_dir, world_dir):
    """Return one reorder row per addon, with BP/RP sides linked together."""
    pack_lookup = installed_pack_lookup(server_dir)
    groups = {}
    for kind, path in world_pack_paths(world_dir).items():
        entries = read_pack_list(path)
        for index, entry in enumerate(entries):
            pack_id = entry.get("pack_id", "")
            installed = pack_lookup.get((pack_id, kind)) or {}
            name = installed.get("name") or f"Unknown pack {pack_id[:8]}"
            key = addon_group_key_from_name(name, pack_id or f"{kind}:{index}")
            group = groups.setdefault(key, {
                "names": [],
                "items": {},
                "orders": [],
            })
            group["names"].append(addon_display_name(name))
            group["items"][kind] = {
                "entry": entry,
                "pack_id": pack_id,
                "name": name,
                "version": entry.get("version"),
                "source": pack_source_label(world_dir, pack_id, kind),
            }
            group["orders"].append((index, 0 if kind == "bp" else 1))

    rows = []
    for group in groups.values():
        rows.append({
            "name": min(group["names"], key=lambda value: (len(value), value.lower())),
            "items": group["items"],
            "order": min(group["orders"]),
        })
    return sorted(rows, key=lambda row: (row["order"], row["name"].lower()))


def reorder_entry_label(row):
    kind_text = addon_kind_label(row["items"])
    details = []
    for kind in ("bp", "rp"):
        item = row["items"].get(kind)
        if not item:
            continue
        pack_id = item.get("pack_id") or ""
        version = version_sort_value(item.get("version"))
        label = kind.upper()
        details.append(f"{label} {pack_id[:8]} | v{version}")
    detail_text = f" {c_gray('(' + '; '.join(details) + ')')}" if details else ""
    return f"{kind_text} {row['name']}{format_group_versions(row)}{source_suffix_for_items(row['items'])}{detail_text}"

def move_selected(entries, selected, direction):
    selected = set(selected)
    order = sorted(selected) if direction < 0 else sorted(selected, reverse=True)
    for index in order:
        target = index + direction
        if target < 0 or target >= len(entries) or target in selected:
            continue
        entries[index], entries[target] = entries[target], entries[index]
        selected.remove(index)
        selected.add(target)
    return selected

def render_reorder_picker(entries, selected, cursor):
    clear_screen()
    ui_menu("Reorder world addons", [
        ("Items", plural(len(entries), "addon")),
        ("Selected", plural(len(selected), "addon")),
        ("Mode", "BP and RP sides move together"),
    ])
    print()
    for i, row in enumerate(entries):
        ui_checkbox_row(i + 1, reorder_entry_label(row), i in selected, i == cursor)
    print()
    ui_help("↑/↓ move", "Space select", "Enter save")
    ui_help("a all", "c clear", "q cancel")


def reorder_packs_keyboard(entries):
    cursor = 0
    selected = set()
    while True:
        render_reorder_picker(entries, selected, cursor)
        key = get_key()
        if key == "up":
            if selected:
                selected = move_selected(entries, selected, -1)
                cursor = min(selected) if selected else cursor
            else:
                cursor = (cursor - 1) % len(entries)
        elif key == "down":
            if selected:
                selected = move_selected(entries, selected, 1)
                cursor = max(selected) if selected else cursor
            else:
                cursor = (cursor + 1) % len(entries)
        elif key == "space":
            if cursor in selected:
                selected.remove(cursor)
            else:
                selected.add(cursor)
        elif key == "a":
            selected = set(range(len(entries)))
        elif key == "c":
            selected.clear()
        elif key == "enter":
            print()
            return True
        elif key == "q":
            print()
            return False


def reorder_packs_text(entries):
    selected = set()
    while True:
        ui_menu("Reorder world addons", [
            ("Items", plural(len(entries), "addon")),
            ("Selected", plural(len(selected), "addon")),
            ("Mode", "BP and RP sides move together"),
        ])
        for i, row in enumerate(entries, 1):
            ui_checkbox_row(i, reorder_entry_label(row), i - 1 in selected)
        print()
        ui_kv("Input", "Toggle numbers, example: 1 or 1,3")
        ui_help("u up", "d down", "a all", "c clear", "Enter save", "q cancel")
        choice = ask("Reorder command", "").lower()
        if choice == "":
            return True
        if choice == "q":
            return False
        if choice == "a":
            selected = set(range(len(entries)))
            continue
        if choice == "c":
            selected.clear()
            continue
        if choice == "u":
            selected = move_selected(entries, selected, -1)
            continue
        if choice == "d":
            selected = move_selected(entries, selected, 1)
            continue
        ok = True
        for part in [p.strip() for p in choice.split(",") if p.strip()]:
            if not part.isdigit():
                ok = False
                break
            index = int(part) - 1
            if index < 0 or index >= len(entries):
                ok = False
                break
            if index in selected:
                selected.remove(index)
            else:
                selected.add(index)
        if not ok:
            ui_status("warn", "Invalid choice.")


def split_combined_reorder_entries(entries):
    split = {"bp": [], "rp": []}
    for row in entries:
        for kind in ("bp", "rp"):
            item = row["items"].get(kind)
            if item:
                split[kind].append(item["entry"])
    return split


def save_combined_world_order(world_dir, entries):
    paths = world_pack_paths(world_dir)
    split = split_combined_reorder_entries(entries)
    backups = {}
    for kind, path in paths.items():
        if path.exists() or split[kind]:
            backups[path] = write_text(path, json.dumps(split[kind], indent=2) + "\n")
    return backups


def rollback_reorder_backups(backups):
    for path, backup in backups.items():
        rollback_path(path, backup)


def reorder_addon_flow(server_dir):
    world_dir = choose_existing_world(server_dir)
    if not world_dir:
        return
    entries = combined_world_reorder_entries(server_dir, world_dir)
    if not entries:
        ui_status("warn", f"No world pack JSON entries found in {world_dir.name}.")
        return
    if sys.stdin.isatty():
        should_save = reorder_packs_keyboard(entries)
    else:
        should_save = reorder_packs_text(entries)
    if not should_save:
        ui_status("warn", "Cancelled. Order was not changed.")
        return
    backups = save_combined_world_order(world_dir, entries)
    if DRY_RUN:
        ui_status("warn", "Dry-run: would save reordered Behavior Packs and Resource Packs.")
        ui_status("info", "Returning to choose action.")
        return

    committed = False
    try:
        ui_status("ok", "Saved reordered Behavior Packs and Resource Packs.")
        for path, backup in backups.items():
            ui_kv("Saved", path)
            if backup:
                ui_kv("Backup", backup)
        committed = yes_no("Commit this reorder change?", True)
    except KeyboardInterrupt:
        rollback_reorder_backups(backups)
        ui_status("warn", "Reorder cancelled. Restored previous order.")
        raise
    if not committed:
        rollback_reorder_backups(backups)
        ui_status("warn", "Reorder discarded. Restored previous order.")
    else:
        ui_status("ok", "Reorder committed.")
    ui_status("info", "Returning to Choose Action.")



def group_uninstall_candidates(candidates):
    groups = {}
    for index, pack in enumerate(candidates):
        key = addon_group_key_from_name(pack["name"], pack.get("pack_id") or pack["name"])
        group = groups.setdefault(key, {
            "names": [],
            "items": {},
            "packs": [],
            "order": index,
        })
        group["names"].append(addon_display_name(pack["name"]))
        group["items"][pack["kind"]] = pack
        group["packs"].append(pack)
        group["order"] = min(group["order"], index)

    result = []
    for group in groups.values():
        group["name"] = min(group["names"], key=lambda value: (len(value), value.lower()))
        result.append(group)
    return sorted(result, key=lambda group: (group["order"], group["name"].lower()))


def addon_group_label(group):
    return f"{addon_kind_label(group['items'])} {group['name']}{format_group_versions(group)}"


def render_checkbox_picker_addons(candidates, selected, cursor):
    clear_screen()
    ui_menu("Select addon to uninstall", [
        ("Found", plural(len(candidates), "addon")),
        ("Selected", plural(len(selected), "addon")),
    ])
    print()
    for i, group in enumerate(candidates):
        ui_checkbox_row(i + 1, addon_group_label(group), i in selected, i == cursor)
    print()
    ui_help("↑/↓ move", "Space select", "Enter remove")
    ui_help("a all", "c clear", "q cancel")

def choose_addons_keyboard(candidates):
    if not candidates:
        return []
    cursor = 0
    selected = set()
    while True:
        render_checkbox_picker_addons(candidates, selected, cursor)
        key = get_key()
        if key == "up":
            cursor = (cursor - 1) % len(candidates)
        elif key == "down":
            cursor = (cursor + 1) % len(candidates)
        elif key == "space":
            if cursor in selected:
                selected.remove(cursor)
            else:
                selected.add(cursor)
        elif key == "enter":
            print()
            return [candidates[i] for i in sorted(selected)]
        elif key == "a":
            selected = set(range(len(candidates)))
        elif key == "c":
            selected.clear()
        elif key == "q":
            print()
            return []

def choose_addons_text(candidates):
    selected = set()
    while True:
        ui_menu("Select addon to uninstall", [
            ("Found", plural(len(candidates), "addon")),
            ("Selected", plural(len(selected), "addon")),
        ])
        for i, group in enumerate(candidates, 1):
            ui_checkbox_row(i, addon_group_label(group), i in selected)
        print()
        ui_kv("Input", "Toggle numbers, example: 1 or 1,3")
        ui_help("a all", "c clear", "Enter continue")

        choice = ask("Choose addon to delete", "")
        if choice == "":
            break
        if choice.lower() == "a":
            selected = set(range(1, len(candidates) + 1))
            continue
        if choice.lower() == "c":
            selected.clear()
            continue
        
        ok = True
        for part in [x.strip() for x in choice.split(",") if x.strip()]:
            if not part.isdigit():
                ok = False
                break
            index = int(part)
            if index < 1 or index > len(candidates):
                ok = False
                break
            if index in selected:
                selected.remove(index)
            else:
                selected.add(index)
        if not ok:
            ui_status("warn", "Invalid choice.")
    
    return [candidates[i - 1] for i in sorted(selected)]

def disable_pack_in_world(world_dir, pack):
    """Remove pack from world JSON config silently, only logging to file."""
    if not pack.get("pack_id"):
        return False
    file_name = "world_resource_packs.json" if pack["kind"] == "rp" else "world_behavior_packs.json"
    path = world_dir / file_name
    if not path.exists():
        return False
    packs = read_pack_list(path)
    original_len = len(packs)
    packs = [p for p in packs if p.get("pack_id") != pack["pack_id"]]
    if len(packs) < original_len:
        log.info("Remove from %s: %s", path.name, pack["name"])
        write_text(path, json.dumps(packs, indent=2) + "\n")
        return True
    return False

def uninstall_addon_flow(server_dir, force_delete=False):
    if not DRY_RUN:
        ui_kv("Tip", "Run with --dry-run first to preview uninstall changes.")
    candidates = get_installed_addons(server_dir)
    if not candidates:
        ui_status("warn", "No installed addons found.")
        return

    world_dir = choose_existing_world(server_dir)
    if world_dir:
        candidates = sort_addons_for_uninstall(candidates, world_dir)
        ui_status("info", f"Sorted uninstall list by pack order in world: {world_dir.name}")
    else:
        ui_status("warn", "Using folder/name order because no world was selected.")
    
    grouped_candidates = group_uninstall_candidates(candidates)
    if sys.stdin.isatty():
        selected_groups = choose_addons_keyboard(grouped_candidates)
    else:
        selected_groups = choose_addons_text(grouped_candidates)

    if not selected_groups:
        ui_status("warn", "Cancelled. Nothing was removed.")
        return
    to_remove = [pack for group in selected_groups for pack in group["packs"]]

    delete_label = "permanently deleted" if force_delete else "moved to backup folders"
    if force_delete:
        ui_panel("Warning: permanent deletion", [
            f"{ui_badge('DANGER', 'danger')} Selected addon folders will be deleted permanently.",
            "Recovery is only possible from your own backups.",
        ], kind="danger")
    else:
        backup_root = Path(__file__).resolve().parent / ".temp-addonInstaller" / "backups" / clean_name(server_dir.name)
        ui_panel("Uninstall preview", [
            f"{ui_badge('INFO', 'info')} Selected addon folders will be moved to backups.",
            ("Backup root", backup_root),
        ])
    print(c_divider("Selected for removal"))
    for i, group in enumerate(selected_groups, 1):
        print(f"  {ui_badge(str(i), 'danger')} {c_bold(addon_group_label(group))}")
    
    confirm = ask(f"\nType {c_bold('DELETE')} to confirm, or press Enter to cancel")
    if confirm != "DELETE":
        ui_status("warn", "Cancelled.")
        return

    worlds_dir = server_dir / "worlds"
    existing_worlds = visible_folders(worlds_dir)
    
    total = len(to_remove)
    total_addons = len(selected_groups)
    removed_names = []

    action_label = "Deleting" if force_delete else "Removing"
    print(c_divider(f"{action_label} {plural(total_addons, 'addon')} ({plural(total, 'folder')})"))
    for idx, pack in enumerate(to_remove, 1):
        pack_path = pack["path"]
        kind_label = "RP" if pack["kind"] == "rp" else "BP"
        backup = uninstall_backup_path(server_dir, pack_path, pack["kind"])
        
        print(f"\n  {ui_badge(f'{idx}/{total}', 'accent')} {c_bold(pack['name'])} {ui_badge(kind_label, 'info')}")
        ui_kv("Folder", pack_path)
        if not force_delete:
            ui_kv("Backup", backup)
        
        # Remove addon folder. Default is backup move; --force-delete is permanent.
        if not DRY_RUN and pack_path.exists():
            if force_delete:
                log.info("Permanently delete: %s", pack_path)
                shutil.rmtree(pack_path)
                ui_status("ok", "Folder deleted.")
            else:
                backup.parent.mkdir(parents=True, exist_ok=True)
                log.info("Move to backup: %s -> %s", pack_path, backup)
                shutil.move(str(pack_path), str(backup))
                ui_status("ok", f"Moved to backup: {backup}")
        elif DRY_RUN:
            if force_delete:
                ui_status("warn", f"Dry-run: folder would be {delete_label}.")
            else:
                ui_status("warn", f"Dry-run: folder would be moved to backup: {backup}")
        
        # Remove from world config
        cleaned_worlds = 0
        for w in existing_worlds:
            if disable_pack_in_world(w, pack):
                cleaned_worlds += 1
        if cleaned_worlds > 0:
            ui_status("ok", f"Removed from {plural(cleaned_worlds, 'world')}.")
        
        removed_names.append(f"[{kind_label}] {pack['name']}")
    
    # Final summary
    total_label = "deleted" if force_delete else "removed"
    ui_panel("Uninstall complete", [
        (f"Total {total_label}", f"{plural(total_addons, 'addon')} ({plural(total, 'folder')})"),
    ], kind="ok")
    for group in selected_groups:
        ui_status("ok", addon_group_label(group))
    print(c_divider())
    ui_status("ok", "Restart bedrock_server to apply changes.")
    print()


def parse_args():
    parser = argparse.ArgumentParser(description="Setup Bedrock RP/BP addons into a server folder.")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without writing files.")
    parser.add_argument(
        "--force-delete",
        action="store_true",
        help="Permanently delete addon folders during uninstall instead of moving them to centralized backups.",
    )
    parser.add_argument("--inspect", metavar="PATH", help="Inspect addon archive contents without installing.")
    return parser.parse_args()


def main():
    global DRY_RUN
    # Reconfigure stdout/stderr to use UTF-8 to prevent encoding errors on Windows console
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    args = parse_args()
    DRY_RUN = args.dry_run

    log_file = setup_logging(DRY_RUN or bool(args.inspect))
    _enable_colors()
    if DRY_RUN:
        log.info("DRY-RUN mode enabled")

    if args.inspect:
        inspect_archive(Path(args.inspect).expanduser().resolve())
        return

    ui_banner(log_file, args.force_delete)

    # Step 1: Choose server
    ui_step(1, "Choose server", "Pick Bedrock server folder containing server.properties.")
    server_dir = choose_server_dir()
    ui_status("ok", f"Server selected: {server_dir}")
    print_server_info(server_dir)

    while True:
        # Step 2: Choose action
        ui_step(2, "Choose action")
        print_installed_addon_overview(server_dir, default_world_dir(server_dir))
        print()
        ui_option("1", "Install addon", "copy packs and enable them in a world")
        ui_option("2", "Uninstall addon", "remove world refs and back up pack folders")
        ui_option("3", "Reorder world addons", "adjust BP/RP load order")
        ui_option("0", "Exit")
        while True:
            action_choice = ask("Choose action", "1", allow_exit=False)
            if is_exit_choice(action_choice):
                ui_status("warn", "Cancelled.")
                return
            if action_choice in ("1", "2", "3"):
                break
            ui_status("warn", "Choose 0, 1, 2, or 3.")

        if action_choice == "2":
            print(c_divider("Uninstall addon"))
            uninstall_addon_flow(server_dir, args.force_delete)
            return
        if action_choice == "3":
            print(c_divider("Reorder world addons"))
            reorder_addon_flow(server_dir)
            continue
        if not DRY_RUN:
            ui_kv("Tip", "Run with --dry-run first when installing unknown addons.")
        break

    # Step 3: Choose addons
    ui_step(3, "Choose addons", "Select one or more .mcpack/.mcaddon/.mctemplate/.zip/.tar.* files.")
    archives = choose_archives(server_dir)
    if not archives:
        ui_status("warn", "No files selected.")
        return
    ui_status("ok", f"{plural(len(archives), 'addon')} selected.")

    installed = []
    imported_worlds = []
    config_changes = []
    archive_results = []  # track per-archive results for summary
    total_archives = len(archives)

    try:
        # Step 4: Process install
        ui_step(4, "Install", f"Processing {plural(total_archives, 'archive')}.")
        batch_context = build_archive_batch_context(archives, server_dir)
        print_archive_batch_overview(batch_context, total_archives)
        conflicts = scan_install_conflicts(archives, server_dir, batch_context)
        print_install_conflicts(conflicts)
        if not confirm_install_conflicts(conflicts):
            ui_status("warn", "Install cancelled before files were copied.")
            return
        for idx, archive in enumerate(archives, 1):
            size_str = f"{archive.stat().st_size / (1024*1024):.1f} MB"
            print(c_divider(f"Archive {idx}/{total_archives}: {archive.name}"))
            ui_kv("Size", size_str)
            packs, worlds = process_archive(archive, server_dir, batch_context)
            installed.extend(packs)
            imported_worlds.extend(worlds)
            for world in worlds:
                config_changes.extend(world.get("config_changes", []))
            archive_results.append((archive.name, packs, worlds))

        if not installed:
            ui_status("warn", "No RP/BP packs were installed.")
            return

        # Step 5: Choose target world
        ui_step(5, "Choose world", "Installed packs will be enabled in this world.")
        world_dir = choose_world(server_dir, imported_worlds)
        ui_status("ok", f"World selected: {world_dir.name}")

        # Step 6: Enable packs in world
        ui_step(6, "Enable packs")
        for pack in installed:
            kind_label = "RP" if pack["kind"] == "rp" else "BP"
            json_file = "world_resource_packs.json" if pack["kind"] == "rp" else "world_behavior_packs.json"
            path, backup = enable_pack(world_dir, pack)
            config_changes.append((path, backup))
            pack_name = pack["name"]
            ui_status("ok", f"[{kind_label}] {pack_name} -> {json_file}")

        if any(pack["kind"] == "rp" for pack in installed):
            if not check_texturepack_required(server_dir):
                if yes_no("\nSet texturepack-required=true so clients automatically download the pack?", True):
                    backup = set_texturepack_required(server_dir)
                    config_changes.append((server_dir / "server.properties", backup))
                    ui_status("ok", "texturepack-required=true")

        # Step 7: Summary
        ui_step(7, "Summary")
        dep_missing = check_dependencies(installed, get_installed_addons(server_dir) + installed)
        print_summary(archive_results, dep_missing)
        print(c_divider())
        ui_status("ok", "Restart bedrock_server to apply changes.")
        ui_kv("Backups", ".temp-addonInstaller/backups/ keeps uninstall backups.")
        print()
    except KeyboardInterrupt:
        rollback_install(installed, imported_worlds, config_changes)
        raise
    except Exception:
        rollback_install(installed, imported_worlds, config_changes)
        raise



if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        ui_status("warn", "Cancelled.")
    except Exception as e:
        ui_status("err", f"Error: {e}")
        log.exception("Fatal error")
        raise SystemExit(1)

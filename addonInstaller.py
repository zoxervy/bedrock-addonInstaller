#!/usr/bin/env python3
import argparse
import json
import logging
import os
import re
import shutil
import sys
import tarfile
import zipfile
from datetime import datetime
from pathlib import Path

PACK_EXTS = {".mcpack", ".mcaddon", ".mctemplate", ".zip"}
TAR_EXTS  = {".tar.gz", ".tgz", ".tar.bz2"}      # supported tar formats
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


# ── Logging setup ────────────────────────────────────────────────────────────
log = logging.getLogger("bedrock_addon")


def setup_logging():
    """Initialize logging to file and stderr."""
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    log_file = None if DRY_RUN else Path("addonInstaller.log")

    if log_file is not None:
        log_file_resolved = log_file.resolve()
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
    return log_file


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
def c_bold(t: str)   -> str: return _c("1",  t)
def c_gray(t: str)   -> str: return _c("90", t)
def c_ok(t: str)     -> str: return c_green(f"\u2713 {t}")
def c_warn(t: str)   -> str: return c_yellow(f"\u26a0 {t}")
def c_err(t: str)    -> str: return c_red(f"\u2717 {t}")
def c_info(t: str)   -> str: return c_cyan(f"\u2192 {t}")
def c_divider(t: str = "") -> str:
    line = "\u2500" * 58
    return c_gray(f"\n{line}") if not t else f"\n{c_bold(t)}\n{c_gray(line)}"


def ui_banner(log_file) -> None:
    """Print the app banner and current runtime mode."""
    width = 58
    title = "Bedrock Addon Installer"
    subtitle = "Install, enable, and remove Minecraft Bedrock addons"
    print()
    print(c_cyan("\u2554" + "\u2550" * width + "\u2557"))
    print(c_cyan("\u2551") + c_bold(f" {title}".ljust(width)) + c_cyan("\u2551"))
    print(c_cyan("\u2551") + c_gray(f" {subtitle}".ljust(width)) + c_cyan("\u2551"))
    print(c_cyan("\u255a" + "\u2550" * width + "\u255d"))
    print(f"  {c_gray('Author')}  @zoxervy")
    if log_file is not None:
        print(f"  {c_gray('Log')}     {log_file.resolve()}")
    else:
        print(f"  {c_gray('Log')}     disabled in dry-run mode")
    if DRY_RUN:
        print(f"  {c_yellow('Mode')}    DRY-RUN, no files will be written")


def ui_option(key: str, label: str, detail: str = "") -> None:
    """Print a menu option with aligned label and detail."""
    suffix = f" {c_gray(detail)}" if detail else ""
    print(f"  {c_cyan(str(key) + ')')} {label}{suffix}")


def ui_hint(text: str) -> None:
    print(f"  {c_gray(text)}")


def ui_empty(title: str, detail: str = "") -> None:
    print(c_warn(title))
    if detail:
        ui_hint(detail)


def plural(count: int, word: str) -> str:
    if count == 1:
        return f"{count} {word}"
    if word.endswith("y"):
        return f"{count} {word[:-1]}ies"
    return f"{count} {word}s"


# ── Progress bar ─────────────────────────────────────────────────────────────
def print_progress(current: int, total: int, label: str = "", bar_width: int = 28) -> None:
    """Show a progress bar on the same line using carriage-return overwrite."""
    if total == 0:
        return
    pct  = current / total
    done = int(bar_width * pct)
    bar  = "\u2588" * done + "\u2591" * (bar_width - done)  # █ and ░
    bar_str   = c_cyan(bar) if _COLOR_ENABLED else bar
    label_str = (label[:22] + "\u2026") if len(label) > 23 else label.ljust(23)
    gray_label = c_gray(label_str) if _COLOR_ENABLED else label_str
    sys.stdout.write(f"\r  [{bar_str}] {pct:5.1%} ({current}/{total}) {gray_label}")
    sys.stdout.flush()
    if current >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()


def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raise KeyboardInterrupt
    return value or default


def yes_no(prompt, default=True):
    d = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} [{d}]: ").strip().lower()
        if not value:
            return default
        if value in ("y", "yes", "ya", "iya"):
            return True
        if value in ("n", "no"):
            return False
        print("Answer y/n.")


def action(text: str) -> None:
    if DRY_RUN:
        msg = c_yellow(f"[DRY-RUN] {text}")
    else:
        msg = c_info(text) if _COLOR_ENABLED else text
    print(msg)
    log.info(text)


def timestamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def backup_path(path):
    return path.with_name(f"{path.name}.bak-{timestamp()}")


def backup_existing(path):
    if not path.exists():
        return None
    backup = backup_path(path)
    log.info("Backup: %s -> %s", path, backup)
    if not DRY_RUN:
        if path.is_dir():
            shutil.copytree(path, backup)
        else:
            shutil.copy2(path, backup)
    return backup


def read_server_level_name(server_dir):
    props = server_dir / "server.properties"
    if not props.exists():
        return None
    for line in props.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("level-name="):
            return line.split("=", 1)[1].strip() or None
    return None


def validate_server_dir(server_dir):
    missing = []
    if not (server_dir / "server.properties").exists():
        missing.append("server.properties")
    binary_name = "bedrock_server.exe" if sys.platform == "win32" else "bedrock_server"
    if not (server_dir / binary_name).exists():
        missing.append(binary_name)
    if missing:
        raise RuntimeError(f"This folder is not a valid/complete Bedrock server. Missing: {', '.join(missing)}")


def choose_server_dir():
    """Choose a Bedrock server folder through an interactive menu."""
    current = Path.cwd().resolve()
    while True:
        print(f"\n{c_bold('Server location')}")
        ui_hint(f"Current folder: {current}")
        ui_option("1", "Use current folder", f"{current.name}/")
        ui_option("2", "Browse subfolders")
        ui_option("3", "Enter manual path")
        choice = ask("Choose option", "1")

        try:
            if choice == "1":
                server_dir = current
            elif choice == "2":
                folders = sorted([p for p in current.iterdir() if p.is_dir()], key=lambda p: p.name.lower())
                if not folders:
                    ui_empty("No folders found.", "Enter a manual Bedrock server path instead.")
                    continue
                print(f"\n{c_bold('Available folders')}")
                for idx, folder in enumerate(folders, 1):
                    ui_option(str(idx), folder.name)
                raw = ask("Choose folder number")
                try:
                    index = int(raw)
                except (TypeError, ValueError):
                    print(c_warn("Choice must be a number."))
                    continue
                if index < 1 or index > len(folders):
                    print(c_warn("Invalid folder choice."))
                    continue
                server_dir = folders[index - 1]
            elif choice == "3":
                manual = ask("Bedrock server folder path", str(current))
                server_dir = Path(manual).expanduser().resolve()
            else:
                print(c_warn("Choose 1, 2, or 3."))
                continue

            if not server_dir.exists():
                print(c_warn(f"Server folder does not exist: {server_dir}"))
                continue
            validate_server_dir(server_dir)
            return server_dir
        except RuntimeError as e:
            print(c_err(f"Error: {e}") if _COLOR_ENABLED else f"Error: {e}")
            ui_hint("Expected files: server.properties and bedrock_server(.exe). Use option 3 if the server is elsewhere.")


def validate_uuid(uuid, context=""):
    """Validate standard UUID format (8-4-4-4-12 hex)."""
    label = f" in {context}" if context else ""
    if not uuid or not UUID_RE.match(uuid):
        raise RuntimeError(f"Invalid UUID{label}: {uuid!r}")


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



def safe_copytree(src: Path, dest: Path) -> None:
    """Copy src directory to dest with backup, disk check, and progress bar."""
    if dest.exists():
        if yes_no(f"Folder {dest.name} already exists. Replace?", True):
            backup_existing(dest)
            log.info("Remove: %s", dest)
            if not DRY_RUN:
                shutil.rmtree(dest)
        else:
            raise RuntimeError("Cancelled because the pack/world folder already exists.")
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
        log.info("Copy complete: %s \u2192 %s (%d files)", src, dest, total)


def write_text(path, content):
    if path.exists():
        backup_existing(path)
    log.info("Write: %s", path)
    if not DRY_RUN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        log.debug("Write file: %s (%d bytes)", path, len(content.encode()))


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


def clean_name(name):
    cleaned = "".join(c if c.isalnum() or c in "._- " else "_" for c in name).strip()
    return cleaned.replace(" ", "_") or "pack"


def pack_folder_name(pack_dir, manifest):
    header = manifest.get("header", {})
    name = header.get("name") or pack_dir.name
    uuid = header.get("uuid", "")[:8]
    return f"{clean_name(name)}_{uuid}" if uuid else clean_name(name)


def find_manifests(root):
    return sorted(root.rglob("manifest.json"))


def scan_addon_content(root):
    """Scan manifests and world markers once so large addons are not scanned repeatedly."""
    manifests = []
    worlds = set()
    markers = set(WORLD_MARKERS)
    action(f"Scan addon content: {root}")
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
        print(f"         {c_ok(f'{nested_count} sub-pack extracted')}")


def temp_extract_dir(archive: Path) -> Path:
    """Local extract folder: .temp-addonInstaller/<addon-name>."""
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
        print(f"\n{c_bold('Addon source')}")
        ui_hint(f"Supported: {', '.join(sorted(PACK_EXTS))}, tar archives")
        ui_option("1", "Browse local folders", f"inside {cwd.name}/")
        ui_option("2", "Scan server folder", f"{server_dir.name}/")
        ui_option("3", "Enter manual folder/file path")
        choice = ask("Choose option", "1")

        if choice == "1":
            folders = sorted([p for p in cwd.iterdir() if p.is_dir()], key=lambda p: p.name.lower())
            # Hide internal folders
            folders = [f for f in folders if not f.name.startswith((".") ) and f.name != "__pycache__"]
            if not folders:
                ui_empty("No folders found.", "Use a manual folder/file path instead.")
                continue
            print(f"\n{c_bold(f'Folders in {cwd.name}/')}")
            for idx, folder in enumerate(folders, 1):
                count = sum(1 for _ in folder.rglob("*") if _.is_file() and is_pack_file(_))
                detail = f"{plural(count, 'archive')}" if count > 0 else "no supported archives"
                ui_option(str(idx), f"{folder.name}/", detail)
            raw = ask("Choose folder number")
            try:
                index = int(raw)
            except (TypeError, ValueError):
                print(c_warn("Choice must be a number."))
                continue
            if index < 1 or index > len(folders):
                print(c_warn("Invalid folder choice."))
                continue
            picked = folders[index - 1]
            pick_count = sum(1 for _ in picked.rglob("*") if _.is_file() and is_pack_file(_))
            if pick_count == 0:
                ui_empty(f"No addon archives found in {picked.name}/.", "Choose another folder or enter a manual file path.")
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
                print(c_warn(f"Path does not exist: {path}"))
                continue
            return path

        print(c_warn("Choose 1, 2, or 3."))


def get_key():
    """Read one key for the interactive picker."""
    if sys.platform == "win32":
        import msvcrt
        key = msvcrt.getch()
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


def render_checkbox_picker(candidates, selected, cursor, search_dirs):
    """Render the interactive addon picker."""
    print("\033[2J\033[H", end="")
    scan_names = ", ".join(d.name for d in search_dirs if d.exists())
    print(c_divider("Select addons"))
    ui_hint(f"Found {plural(len(candidates), 'archive')} \u00b7 Scan: {scan_names}")
    for i, path in enumerate(candidates):
        pointer = c_cyan("\u203a") if i == cursor else " "
        mark = c_green("\u2713") if i in selected else " "
        folder = c_gray(f'({path.parent.name}/)')
        print(f"  {pointer} [{mark}] {i + 1}. {path.name} {folder}")
    print()
    ui_hint("\u2191/\u2193 move \u00b7 Space select \u00b7 Enter install")
    ui_hint("a all \u00b7 c clear \u00b7 r refresh \u00b7 m manual file \u00b7 q cancel")


def choose_archives_keyboard(candidates, search_dirs):
    """Choose addons with arrow keys + Space."""
    if not candidates:
        return []
    cursor = 0
    selected = set()
    while True:
        render_checkbox_picker(candidates, selected, cursor, search_dirs)
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
        elif key == "r":
            old_selected_paths = {candidates[i] for i in selected}
            candidates.clear()
            candidates.extend(list_archives(search_dirs))
            selected.clear()
            for i, path in enumerate(candidates):
                if path in old_selected_paths:
                    selected.add(i)
            cursor = min(cursor, max(len(candidates) - 1, 0))
        elif key == "m":
            print()
            manual = ask("File path")
            if manual:
                manual_path = Path(manual).expanduser().resolve()
                if not manual_path.exists():
                    print(c_warn(f"File does not exist: {manual_path}"))
                    input("Press Enter to continue...")
                    continue
                if not manual_path.is_file() or not is_pack_file(manual_path):
                    print(c_warn(f"File is not a Bedrock addon/template/archive: {manual_path}"))
                    input("Press Enter to continue...")
                    continue
                candidates.append(manual_path)
                selected.add(len(candidates) - 1)
                cursor = len(candidates) - 1
        elif key == "q":
            print()
            return []


def choose_archives_text(candidates, search_dirs):
    """Text-based addon picker fallback."""
    selected = set()
    while True:
        print(c_divider("Select addons"))
        ui_hint(f"Found {plural(len(candidates), 'archive')}")
        for i, path in enumerate(candidates, 1):
            mark = c_green("✓") if i in selected else " "
            folder = c_gray(f'({path.parent.name}/)')
            print(f"  [{mark}] {i}. {path.name} {folder}")
        print()
        ui_hint("Numbers toggle selection, example: 1 or 1,3")
        ui_hint("a all · c clear · r refresh · 0 manual file · empty install")

        choice = ask("Choose addon/template", "")
        if choice == "":
            break
        if choice.lower() == "a":
            selected = set(range(1, len(candidates) + 1))
            continue
        if choice.lower() == "c":
            selected.clear()
            continue
        if choice.lower() == "r":
            old_selected_paths = {candidates[i - 1] for i in selected}
            candidates.clear()
            candidates.extend(list_archives(search_dirs))
            selected.clear()
            for i, path in enumerate(candidates, 1):
                if path in old_selected_paths:
                    selected.add(i)
            print(c_ok(f"Refreshed: {plural(len(candidates), 'archive')} found."))
            continue
        if choice == "0":
            manual = ask("File path")
            if manual:
                manual_path = Path(manual).expanduser().resolve()
                if not manual_path.exists():
                    print(c_warn(f"File does not exist: {manual_path}"))
                    continue
                if not manual_path.is_file() or not is_pack_file(manual_path):
                    print(c_warn(f"File is not a Bedrock addon/template/archive: {manual_path}"))
                    continue
                candidates.append(manual_path)
                selected.add(len(candidates))
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
                selected.add(index)
        if not ok:
            print(c_warn("Invalid choice. Use numbers, example: 1 or 1,3."))

    return [candidates[i - 1] for i in sorted(selected)]


def choose_archives(server_dir):
    while True:
        source = choose_archive_location(server_dir)
        if source.is_file():
            if not is_pack_file(source):
                print(c_warn(f"File is not a Bedrock addon/template/archive: {source}"))
                continue
            return [source]

        search_dirs = [source]
        candidates = list_archives(search_dirs)
        if candidates:
            break
        ui_empty(f"No supported addon archives found in {source.name}/.", "Choose another location or enter a manual file path.")

    if sys.stdin.isatty():
        return choose_archives_keyboard(candidates, search_dirs)
    return choose_archives_text(candidates, search_dirs)



def manifest_dependencies(manifest):
    deps = []
    for dep in manifest.get("dependencies", []):
        uuid = dep.get("uuid")
        version = dep.get("version")
        if uuid:
            deps.append({"uuid": uuid, "version": version})
    return deps


def install_pack_dir(pack_dir, manifest, server_dir):
    kinds = detect_pack_kinds(manifest)
    if not kinds:
        msg = f"Skip {pack_dir}: not a Bedrock RP/BP"
        print(msg)
        log.warning(msg)
        return []

    header = manifest.get("header", {})
    pack_id = header.get("uuid")
    # Validate UUID format before use
    validate_uuid(pack_id, context=str(pack_dir))
    version = version_array(header.get("version"))
    log.info("Pack found: %s | kind=%s | uuid=%s | version=%s",
             header.get('name', pack_dir.name), kinds, pack_id, version)

    installed = []
    for kind in kinds:
        base = server_dir / ("resource_packs" if kind == "rp" else "behavior_packs")
        if not DRY_RUN:
            base.mkdir(parents=True, exist_ok=True)
        else:
            action(f"Ensure dir: {base}")
        dest = base / pack_folder_name(pack_dir, manifest)
        safe_copytree(pack_dir, dest)
        installed.append({
            "pack_id": pack_id,
            "version": version,
            "path": str(dest),
            "name": header.get("name", pack_dir.name),
            "kind": kind,
            "dependencies": manifest_dependencies(manifest),
        })
    return installed


def import_world_dir(src_world, server_dir):
    worlds_dir = server_dir / "worlds"
    if not DRY_RUN:
        worlds_dir.mkdir(parents=True, exist_ok=True)
    else:
        action(f"Ensure dir: {worlds_dir}")

    levelname_file = src_world / "levelname.txt"
    default_name = levelname_file.read_text(errors="ignore").strip() if levelname_file.exists() else src_world.name
    default_name = default_name or src_world.name
    world_name = safe_world_name(ask(f"World name to import from template {src_world.name}", default_name))
    dest = safe_child_path(worlds_dir, world_name, world_name)
    safe_copytree(src_world, dest)
    return dest


def load_manifests_from_archive(archive: Path):
    """Read manifest.json directly from the archive without full extraction."""
    manifests = []
    if is_tar_file(archive):
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                if Path(member.name).name != "manifest.json" or not member.isfile():
                    continue
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src:
                    manifests.append((member.name, json.loads(src.read().decode("utf-8-sig"))))
    else:
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                if Path(name).name != "manifest.json":
                    continue
                with z.open(name) as src:
                    manifests.append((name, json.loads(src.read().decode("utf-8-sig"))))
    return manifests


def dry_run_install_manifest(manifest_name: str, manifest: dict, server_dir: Path):
    """Simulate pack installation from manifest without full archive extraction."""
    installed = []
    header = manifest.get("header", {})
    pack_id = header.get("uuid")
    version = version_array(header.get("version"))
    validate_uuid(pack_id, f"manifest {manifest_name} header.uuid")
    kinds = detect_pack_kinds(manifest)
    if not kinds:
        print(f"Skip manifest without resource/data module: {manifest_name}")
        return installed

    for kind in kinds:
        base = server_dir / ("resource_packs" if kind == "rp" else "behavior_packs")
        action(f"Ensure dir: {base}")
        virtual_pack_dir = Path(manifest_name).parent
        if str(virtual_pack_dir) in ("", "."):
            virtual_pack_dir = Path(archive_stem_from_manifest(manifest_name))
        dest = base / pack_folder_name(virtual_pack_dir, manifest)
        action(f"Would install: {manifest_name} → {dest}")
        installed.append({
            "pack_id": pack_id,
            "version": version,
            "path": str(dest),
            "name": header.get("name", virtual_pack_dir.name),
            "kind": kind,
            "dependencies": manifest_dependencies(manifest),
        })
    return installed


def archive_stem_from_manifest(manifest_name: str) -> str:
    parent = Path(manifest_name).parent
    return parent.name if str(parent) not in ("", ".") else "pack"


def process_archive(archive, server_dir):
    archive = Path(archive).expanduser().resolve()
    # Validate archive size and integrity before extraction
    validate_archive(archive)
    size_mb = archive.stat().st_size / (1024 * 1024)

    if DRY_RUN:
        print(f"         {c_yellow('[DRY-RUN] Reading manifest without full extraction')}")
        installed = []
        for manifest_name, manifest in load_manifests_from_archive(archive):
            installed.extend(dry_run_install_manifest(manifest_name, manifest, server_dir))
        return installed, []

    # Step: Extract
    print(f"         {c_info(f'Extracting ({size_mb:.1f} MB)...')}")
    tmp = extract_archive_to_temp(archive)
    installed = []
    imported_worlds = []
    try:
        # Step: Scan content
        manifests, world_dirs = scan_addon_content(tmp)
        pack_count = len(manifests)
        print(f"         {c_ok(f'{pack_count} pack(s) found')}")

        # Step: Install pack
        for manifest_path in manifests:
            manifest = load_json(manifest_path)
            header = manifest.get("header", {})
            pack_name = header.get("name", manifest_path.parent.name)
            kinds = detect_pack_kinds(manifest)
            kind_labels = "/".join(k.upper() for k in kinds) if kinds else "?"
            result = install_pack_dir(manifest_path.parent, manifest, server_dir)
            if result:
                print(f"         {c_ok(f'Install {kind_labels}: {pack_name}')}")
            installed.extend(result)

        for world_dir in world_dirs:
            if yes_no(f"Template/world detected: {world_dir.name}. Import world?", True):
                imported_worlds.append(import_world_dir(world_dir, server_dir))
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
        print(f"Broken JSON: {path}. It will be rewritten as an empty list plus new packs.")
        return []


def enable_pack(world_dir, installed):
    file_name = "world_resource_packs.json" if installed["kind"] == "rp" else "world_behavior_packs.json"
    path = world_dir / file_name
    packs = read_pack_list(path)
    packs = [p for p in packs if p.get("pack_id") != installed["pack_id"]]
    packs.append({"pack_id": installed["pack_id"], "version": installed["version"]})
    write_text(path, json.dumps(packs, indent=2) + "\n")
    return path


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
    write_text(props, "\n".join(new_lines) + "\n")


def print_world_choices(title, worlds):
    print(f"\n{c_bold(title)}")
    for i, world in enumerate(worlds, 1):
        ui_option(str(i), world.name)


def choose_world(server_dir, imported_worlds):
    if imported_worlds:
        print_world_choices("Imported worlds", imported_worlds)
        if yes_no("Use an imported world to enable packs?", True):
            if len(imported_worlds) == 1:
                return imported_worlds[0]
            while True:
                raw = ask("Choose imported world", "1")
                try:
                    idx = int(raw)
                    if 1 <= idx <= len(imported_worlds):
                        return imported_worlds[idx - 1]
                    print(c_warn(f"Choose a number 1-{len(imported_worlds)}."))
                except ValueError:
                    print(c_warn("Enter a valid number."))

    worlds_dir = server_dir / "worlds"
    if not DRY_RUN:
        worlds_dir.mkdir(parents=True, exist_ok=True)
    else:
        action(f"Ensure dir: {worlds_dir}")
    existing = sorted([p for p in worlds_dir.iterdir() if p.is_dir()]) if worlds_dir.exists() else []

    if existing:
        print_world_choices("World folders", existing)
        ui_option("0", "Create/use another name")
        while True:
            choice = ask("Choose world", "1")
            try:
                idx = int(choice)
                if idx == 0:
                    break
                if 1 <= idx <= len(existing):
                    return existing[idx - 1]
                print(c_warn(f"Choose a number 0-{len(existing)}."))
            except ValueError:
                print(c_warn("Enter a valid number."))

    prop_name = read_server_level_name(server_dir)
    default_name = prop_name or "Bedrock level"

    ui_empty("No world folders found.", f"Default name from server.properties: {default_name}")

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


def check_dependencies(installed):
    installed_ids = {p["pack_id"] for p in installed}
    missing = []
    for pack in installed:
        for dep in pack.get("dependencies", []):
            if dep["uuid"] not in installed_ids:
                missing.append((pack, dep))
    return missing


def _tick(cond: bool) -> str:
    """Check/cross symbol with color when available."""
    if _COLOR_ENABLED:
        return c_green("[\u2713]") if cond else c_gray("[ ]")
    return "[✓]" if cond else "[ ]"


def print_summary(archive_results, dep_missing) -> None:
    """Print a clean per-archive install summary."""
    for archive_name, packs, worlds in archive_results:
        print(c_divider(f"Summary · {archive_name}"))

        seen = set()
        for p in packs:
            pid = p["pack_id"]
            if pid not in seen:
                seen.add(pid)
                print(f"  {c_ok(p['name'])} {c_gray(pid)}")

        if DRY_RUN:
            print(f"  {c_yellow('Mode')}       dry-run preview")

        bp_packs = [x for x in packs if x["kind"] == "bp"]
        rp_packs = [x for x in packs if x["kind"] == "rp"]
        print(f"  BP packs   {_tick(bool(bp_packs))} {plural(len(bp_packs), 'pack')}")
        for p in bp_packs:
            print(f"    {c_gray('path')}    {p['path']}")
            print(f"    {c_gray('version')} {'.'.join(map(str, p['version']))}")

        print(f"  RP packs   {_tick(bool(rp_packs))} {plural(len(rp_packs), 'pack')}")
        for p in rp_packs:
            print(f"    {c_gray('path')}    {p['path']}")
            print(f"    {c_gray('version')} {'.'.join(map(str, p['version']))}")

        print(f"  Worlds     {_tick(bool(worlds))} {plural(len(worlds), 'import')}")
        for w in worlds:
            print(f"    {c_gray('path')}    {w}")

        pack_ids = {p["pack_id"] for p in packs}
        local_missing = [(p, d) for p, d in dep_missing if p["pack_id"] in pack_ids]
        print(f"  Deps       {_tick(not local_missing)} {plural(len(local_missing), 'missing dependency')}")
        for pack, dep in local_missing:
            print(f"    {c_err(pack['name'])} needs {c_yellow(dep['uuid'])} version {dep.get('version')}")


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
                    "name": header.get("name", pack_dir.name),
                    "pack_id": header.get("uuid"),
                    "kind": kind
                })
            except Exception as e:
                log.warning("Failed to read %s: %s", manifest_path, e)
    return installed

def render_checkbox_picker_addons(candidates, selected, cursor):
    print("\033[2J\033[H", end="")
    print(c_divider("Select addons to delete"))
    ui_hint(f"Found {plural(len(candidates), 'installed addon')}")
    for i, p in enumerate(candidates):
        pointer = c_cyan("›") if i == cursor else " "
        mark = c_green("✓") if i in selected else " "
        kind_str = "RP" if p["kind"] == "rp" else "BP"
        print(f"  {pointer} [{mark}] {i + 1}. [{kind_str}] {p['name']} {c_gray('(' + p['path'].name + ')')}")
    print()
    ui_hint("↑/↓ move · Space select · Enter delete")
    ui_hint("a all · c clear · q cancel")

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
        print(c_divider("Select addons to delete"))
        ui_hint(f"Found {plural(len(candidates), 'installed addon')}")
        for i, p in enumerate(candidates, 1):
            mark = c_green("✓") if i in selected else " "
            kind_str = "RP" if p["kind"] == "rp" else "BP"
            print(f"  [{mark}] {i}. [{kind_str}] {p['name']} {c_gray('(' + p['path'].name + ')')}")
        print()
        ui_hint("Numbers toggle selection, example: 1 or 1,3")
        ui_hint("a all · c clear · empty continue")

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
            print(c_warn("Invalid choice."))
    
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
        if not DRY_RUN:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(packs, indent=2) + "\n", encoding="utf-8")
        return True
    return False

def uninstall_addon_flow(server_dir):
    if not DRY_RUN:
        ui_hint("Tip: run with --dry-run first to preview uninstall changes.")
    candidates = get_installed_addons(server_dir)
    if not candidates:
        ui_empty("No installed addons found.", "Only user-installed packs with manifest.json are shown.")
        return
    
    if sys.stdin.isatty():
        to_remove = choose_addons_keyboard(candidates)
    else:
        to_remove = choose_addons_text(candidates)
        
    if not to_remove:
        print(c_warn("Cancelled, nothing was deleted."))
        return
    
    # Confirmation before deletion - strong warning
    print(f"\n{c_red('!' * 50)}")
    print(f"{c_bold(c_red('  ⚠  WARNING: PERMANENT DELETION  ⚠'))}")
    print(f"{c_red('!' * 50)}")
    print(f"\n{c_bold('Addons to be deleted:')}")
    for i, pack in enumerate(to_remove, 1):
        kind_str = c_cyan("RP") if pack["kind"] == "rp" else c_cyan("BP")
        print(f"  {c_red('✗')} {i}. [{kind_str}] {c_bold(pack['name'])}")
    print(f"\n{c_red('Addon files will be PERMANENTLY DELETED and CANNOT be restored!')}")
    
    confirm = ask(f"\nType {c_bold('DELETE')} to confirm, or press Enter to cancel")
    if confirm != "DELETE":
        print("Cancelled.")
        return

    worlds_dir = server_dir / "worlds"
    existing_worlds = sorted([p for p in worlds_dir.iterdir() if p.is_dir()]) if worlds_dir.exists() else []
    
    total = len(to_remove)
    removed_names = []
    
    print(c_divider(f"Deleting {plural(total, 'addon')}"))
    for idx, pack in enumerate(to_remove, 1):
        pack_path = pack["path"]
        kind_label = "RP" if pack["kind"] == "rp" else "BP"
        
        print(f"\n  [{idx}/{total}] {c_bold(pack['name'])} ({kind_label})")
        
        # Permanently delete folder
        if not DRY_RUN and pack_path.exists():
            log.info("Permanently delete: %s", pack_path)
            shutil.rmtree(pack_path)
            print(f"         {c_ok('Folder deleted')}")
        elif DRY_RUN:
            print(f"         {c_yellow('[DRY-RUN] Would delete')}")
        
        # Remove from world config
        cleaned_worlds = 0
        for w in existing_worlds:
            if disable_pack_in_world(w, pack):
                cleaned_worlds += 1
        if cleaned_worlds > 0:
            print(f"         {c_ok(f'Removed from {cleaned_worlds} world')}")
        
        removed_names.append(f"[{kind_label}] {pack['name']}")
    
    # Final summary
    print(c_divider("Uninstall complete"))
    print(f"  Deleted    {c_green(plural(total, 'addon'))}")
    for name in removed_names:
        print(f"  {c_ok(name)}")
    print(c_divider())
    print(f"  {c_green('\U0001F680 Restart bedrock_server to apply changes.')}")
    print()


def parse_args():
    parser = argparse.ArgumentParser(description="Setup Bedrock RP/BP addons into a server folder.")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without writing files.")
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

    log_file = setup_logging()
    _enable_colors()
    if DRY_RUN:
        log.info("DRY-RUN mode enabled")

    ui_banner(log_file)

    # Step 1: Choose server
    print(c_divider("Step 1: Choose Server"))
    server_dir = choose_server_dir()
    print(f"{c_ok(f'Server: {server_dir.name}/')}")

    # Step 2: Choose action
    print(c_divider("Step 2: Choose Action"))
    ui_option("1", "📥 Install addon", "copy packs and enable them in a world")
    ui_option("2", "🗑  Uninstall addon", "remove user-installed packs")
    while True:
        action_choice = ask("Choose action", "1")
        if action_choice in ("1", "2"):
            break
        print(c_warn("Choose 1 or 2."))

    if action_choice == "2":
        print(c_divider("Uninstall Addon"))
        uninstall_addon_flow(server_dir)
        return

    if not DRY_RUN:
        ui_hint("Tip: run with --dry-run first when installing unknown addons.")

    # Step 3: Choose addons
    print(c_divider("Step 3: Choose Addons"))
    archives = choose_archives(server_dir)
    if not archives:
        print(c_warn("No files selected."))
        return
    print(c_ok(f"{plural(len(archives), 'addon')} selected"))

    installed = []
    imported_worlds = []
    archive_results = []  # track per-archive results for summary
    total_archives = len(archives)

    # Step 4: Process install
    print(c_divider(f"Step 4: Install ({plural(total_archives, 'addon')})"))
    for idx, archive in enumerate(archives, 1):
        size_str = f"{archive.stat().st_size / (1024*1024):.1f} MB"
        print(f"\n  [{idx}/{total_archives}] {c_bold(archive.name)} {c_gray(f'({size_str})')}")
        packs, worlds = process_archive(archive, server_dir)
        installed.extend(packs)
        imported_worlds.extend(worlds)
        archive_results.append((archive.name, packs, worlds))

    if not installed:
        print(f"\n{c_warn('No RP/BP packs were installed.')}")
        return

    # Step 5: Choose target world
    print(c_divider("Step 5: Choose World"))
    ui_hint("Packs are installed. Choose a world to enable them in world JSON files.")
    world_dir = choose_world(server_dir, imported_worlds)
    print(f"{c_ok(f'World: {world_dir.name}')}")

    # Step 6: Enable packs in world
    print(c_divider("Step 6: Enable Packs"))
    for pack in installed:
        kind_label = "RP" if pack["kind"] == "rp" else "BP"
        json_file = "world_resource_packs.json" if pack["kind"] == "rp" else "world_behavior_packs.json"
        enable_pack(world_dir, pack)
        pack_name = pack["name"]
        print(f"  {c_ok(f'[{kind_label}] {pack_name}')} {c_gray('→ ' + json_file)}")

    if any(pack["kind"] == "rp" for pack in installed):
        if not check_texturepack_required(server_dir):
            if yes_no("\nSet texturepack-required=true so clients automatically download the pack?", True):
                set_texturepack_required(server_dir)
                print(f"  {c_ok('texturepack-required=true')}")

    # Step 7: Summary
    dep_missing = check_dependencies(installed)
    print_summary(archive_results, dep_missing)
    print(c_divider())
    print(f"  {c_green('\U0001F680 Restart bedrock_server to apply changes.')}")
    print(f"  {c_gray('Backup files (.bak-*) are kept in case you want to restore them.')}")
    print()



if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{c_yellow('Cancelled.')}")
    except Exception as e:
        print(c_err(f"Error: {e}") if _COLOR_ENABLED else f"Error: {e}")
        log.exception("Fatal error")
        raise SystemExit(1)

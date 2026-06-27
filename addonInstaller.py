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
TAR_EXTS  = {".tar.gz", ".tgz", ".tar.bz2"}      # format tar yang didukung
WORLD_MARKERS = {"level.dat", "levelname.txt"}
DRY_RUN = False
MAX_ARCHIVE_MB = 500
UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE
)


def is_tar_file(path: Path) -> bool:
    """Cek apakah path adalah file tar (mendukung ekstensi ganda .tar.gz)."""
    name = path.name.lower()
    return any(name.endswith(ext) for ext in TAR_EXTS)


def is_pack_file(path: Path) -> bool:
    """Cek apakah path adalah file pack/archive yang didukung."""
    return path.suffix.lower() in PACK_EXTS or is_tar_file(path)

def is_within_dir(base: Path, target: Path) -> bool:
    """Cek target tetap berada di dalam base setelah resolve."""
    try:
        return os.path.commonpath([str(base.resolve()), str(target.resolve())]) == str(base.resolve())
    except ValueError:
        return False


def safe_child_path(base: Path, name: str, label: str) -> Path:
    """Buat path anak yang tidak boleh escape dari base."""
    target = base / name
    if not is_within_dir(base, target):
        raise RuntimeError(f"Path traversal blocked: {label}")
    return target


def safe_world_name(name: str) -> str:
    """Validasi nama world agar tidak bisa menjadi path traversal."""
    cleaned = name.strip()
    if not cleaned:
        raise RuntimeError("Nama world tidak boleh kosong.")
    if Path(cleaned).is_absolute() or Path(cleaned).name != cleaned:
        raise RuntimeError(f"Nama world tidak aman: {name}")
    if cleaned in {".", ".."}:
        raise RuntimeError(f"Nama world tidak aman: {name}")
    return cleaned


# ── Logging setup ────────────────────────────────────────────────────────────
log = logging.getLogger("bedrock_addon")


def setup_logging():
    """Inisialisasi logger ke file dan stdout sekaligus."""
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    log_file = Path("addonInstaller.log")
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
    log.info("Bedrock Addon Setup dimulai")
    log.info("Platform: %s | Python: %s", sys.platform, sys.version.split()[0])
    return log_file


# ── Color utilities ──────────────────────────────────────────────────────────
_COLOR_ENABLED = False


def _enable_colors() -> bool:
    """Aktifkan ANSI color. Di Windows, aktifkan VT mode via ctypes."""
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
    log.info("Warna terminal: aktif")
    return True


def _c(code: str, text: str) -> str:
    """Bungkus text dengan ANSI escape code jika warna aktif."""
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
    line = "\u2500" * 50
    return c_gray(f"\n{line}") if not t else f"\n{c_bold(t)}\n{c_gray(line)}"


# ── Progress bar ─────────────────────────────────────────────────────────────
def print_progress(current: int, total: int, label: str = "", bar_width: int = 28) -> None:
    """Tampilkan progress bar di baris yang sama (carriage-return overwrite)."""
    if total == 0:
        return
    pct  = current / total
    done = int(bar_width * pct)
    bar  = "\u2588" * done + "\u2591" * (bar_width - done)  # █ dan ░
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
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def yes_no(prompt, default=True):
    d = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} [{d}]: ").strip().lower()
        if not value:
            return default
        if value in ("y", "yes", "ya", "iya"):
            return True
        if value in ("n", "no", "tidak", "ga", "gak"):
            return False
        print("Jawab y/n.")


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
        raise RuntimeError(f"Folder ini bukan/kurang server Bedrock. Missing: {', '.join(missing)}")


def choose_server_dir():
    """Pilih folder server Bedrock lewat menu interaktif."""
    current = Path.cwd().resolve()
    while True:
        print(f"\n{c_bold('Currently in :')} [{current}]")
        print(f"  1) Pakai folder ini sebagai directory server ({current.name})")
        print("  2) List semua folder yang ada di sini")
        print("  3) Masukkan manual path")
        choice = ask("Pilih opsi", "1")

        try:
            if choice == "1":
                server_dir = current
            elif choice == "2":
                folders = sorted([p for p in current.iterdir() if p.is_dir()], key=lambda p: p.name.lower())
                if not folders:
                    print("Tidak ada folder di directory ini.")
                    continue
                print("\nFolder tersedia:")
                for idx, folder in enumerate(folders, 1):
                    print(f"  {idx}) {folder.name}")
                raw = ask("Pilih nomor folder")
                try:
                    index = int(raw)
                except (TypeError, ValueError):
                    print("Pilihan harus angka.")
                    continue
                if index < 1 or index > len(folders):
                    print("Pilihan folder tidak valid.")
                    continue
                server_dir = folders[index - 1]
            elif choice == "3":
                manual = ask("Path folder server Bedrock", str(current))
                server_dir = Path(manual).expanduser().resolve()
            else:
                print("Pilihan harus 1, 2, atau 3.")
                continue

            if not server_dir.exists():
                print(f"Folder server tidak ada: {server_dir}")
                continue
            validate_server_dir(server_dir)
            return server_dir
        except RuntimeError as e:
            print(c_err(f"Error: {e}") if _COLOR_ENABLED else f"Error: {e}")


def validate_uuid(uuid, context=""):
    """Validasi format UUID standar (8-4-4-4-12 hex)."""
    label = f" di {context}" if context else ""
    if not uuid or not UUID_RE.match(uuid):
        raise RuntimeError(f"UUID tidak valid{label}: {uuid!r}")


def validate_archive(archive: Path) -> None:
    """Validasi ukuran dan integritas file archive (zip atau tar) sebelum diproses."""
    if not archive.exists():
        raise RuntimeError(f"File tidak ada: {archive}")
    size_mb = archive.stat().st_size / (1024 * 1024)
    log.info("Archive: %s (%.1f MB)", archive.name, size_mb)
    if size_mb > MAX_ARCHIVE_MB:
        raise RuntimeError(
            f"File terlalu besar: {size_mb:.1f} MB (batas {MAX_ARCHIVE_MB} MB). "
            f"Ubah MAX_ARCHIVE_MB jika memang diperlukan."
        )
    if is_tar_file(archive):
        if not tarfile.is_tarfile(archive):
            raise RuntimeError(f"Bukan tar valid: {archive}")
    elif not zipfile.is_zipfile(archive):
        raise RuntimeError(f"Bukan zip/mcpack valid: {archive}")
    log.info("Archive valid: %s (%.1f MB)", archive.name, size_mb)


def check_disk_space(src, dest_parent):
    """Pastikan disk tujuan punya ruang cukup untuk menyalin src."""
    if DRY_RUN:
        return
    needed = sum(f.stat().st_size for f in Path(src).rglob("*") if f.is_file())
    try:
        free = shutil.disk_usage(dest_parent).free
    except OSError:
        log.warning("Tidak bisa cek disk space di %s, skip.", dest_parent)
        return
    needed_mb = needed / (1024 * 1024)
    free_mb = free / (1024 * 1024)
    log.info("Disk check: butuh %.1f MB, tersisa %.1f MB di %s", needed_mb, free_mb, dest_parent)
    if needed > free:
        raise RuntimeError(
            f"Disk tidak cukup: butuh {needed_mb:.1f} MB, "
            f"tersisa {free_mb:.1f} MB di {dest_parent}"
        )


def safe_extract(zip_path: Path, dest: Path) -> None:
    """Ekstrak ZIP dengan proteksi path traversal (zip slip)."""
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
        pass  # progress bar sudah newline sendiri


def safe_extract_tar(tar_path: Path, dest: Path) -> None:
    """Ekstrak TAR (.tar.gz/.tgz/.tar.bz2) dengan proteksi path traversal."""
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
                raise RuntimeError(f"Tidak bisa ekstrak tar member: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            copied += 1
            if total and (copied == total or copied % 25 == 0):
                print_progress(copied, total, Path(member.name).name)
        pass  # progress bar sudah newline sendiri



def safe_copytree(src: Path, dest: Path) -> None:
    """Salin direktori src ke dest dengan backup, disk check, dan progress bar."""
    if dest.exists():
        if yes_no(f"Folder {dest.name} sudah ada. Replace?", True):
            backup_existing(dest)
            log.info("Remove: %s", dest)
            if not DRY_RUN:
                shutil.rmtree(dest)
        else:
            raise RuntimeError("Dibatalkan karena folder pack/world sudah ada.")
    if not DRY_RUN:
        check_disk_space(src, dest.parent)
    log.info("Copy: %s → %s", src, dest)
    if not DRY_RUN:
        # Kumpulkan daftar file dulu untuk progress bar
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
            pass  # direktori kosong, tidak ada yang di-progress
        log.info("Selesai copy: %s \u2192 %s (%d files)", src, dest, total)


def write_text(path, content):
    if path.exists():
        backup_existing(path)
    log.info("Write: %s", path)
    if not DRY_RUN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        log.debug("Tulis file: %s (%d bytes)", path, len(content.encode()))


def load_json(path):
    # utf-8-sig menangani BOM yang kadang muncul di file Windows
    return json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))


def version_array(version):
    if isinstance(version, list):
        result = version
    elif isinstance(version, str):
        result = [int(x) for x in version.split(".")]
    else:
        raise RuntimeError(f"Format version tidak dikenal: {version}")
    if not all(isinstance(x, int) for x in result):
        raise RuntimeError(f"Version harus angka: {version}")
    # Pad atau truncate ke tepat 3 elemen agar toleran terhadap manifest non-standar
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
    """Scan manifest dan world marker sekali jalan agar addon besar tidak discan berulang."""
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
    """Cari dan ekstrak file archive di dalam directory secara rekursif."""
    found_any = True
    nested_count = 0
    failed: set[Path] = set()  # Track archive yang gagal agar tidak diulang
    while found_any:
        if nested_count >= max_depth:
            log.warning("Batas kedalaman nested archive tercapai (%d). Stop.", max_depth)
            break
        found_any = False
        log.info("Scan archive bersarang: %s", directory)
        for path in list(directory.rglob("*")):
            if path.is_file() and is_pack_file(path) and path not in failed:
                dest_dir = path.parent / f"_extracted_{path.stem}"
                log.info("Mengekstrak archive bersarang: %s -> %s", path.name, dest_dir.name)
                try:
                    if is_tar_file(path):
                        safe_extract_tar(path, dest_dir)
                    else:
                        safe_extract(path, dest_dir)
                    path.unlink()
                    nested_count += 1
                    found_any = True
                    break  # Scan ulang setelah modifikasi struktur folder
                except Exception as e:
                    log.warning("Gagal mengekstrak archive bersarang %s: %s", path.name, e)
                    failed.add(path)
    if nested_count > 0:
        print(f"         {c_ok(f'{nested_count} sub-pack diekstrak')}")


def temp_extract_dir(archive: Path) -> Path:
    """Folder extract lokal: .temp-addonInstaller/<nama-addon>."""
    temp_root = Path(__file__).resolve().parent / ".temp-addonInstaller"
    temp_root.mkdir(parents=True, exist_ok=True)
    name = clean_name(archive.name)
    dest = safe_child_path(temp_root, name, name)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def extract_archive_to_temp(archive: Path) -> Path:
    """Ekstrak archive (zip atau tar) ke direktori temp, return path-nya."""
    tmp = temp_extract_dir(archive)
    log.info("Prepare temp: %s", tmp)
    if is_tar_file(archive):
        log.info("Ekstrak tar: %s", archive.name)
        safe_extract_tar(archive, tmp)
    else:
        log.info("Ekstrak zip: %s", archive.name)
        safe_extract(archive, tmp)

    # Proses file archive bersarang (.mcpack/.zip dsb) yang ada di dalam
    process_nested_archives(tmp)
    return tmp


def list_archives(search_dirs) -> list:
    """Cari semua file pack/archive secara rekursif di direktori yang diberikan."""
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
    """Pilih lokasi addon/template lewat menu interaktif."""
    cwd = Path.cwd().resolve()

    while True:
        print(f"\n{c_bold('Lokasi addon/template')}")
        print(f"  1) List folder yang ada di {cwd.name}/")
        print(f"  2) Scan folder server ({server_dir.name}/)")
        print("  3) Masukkan manual path folder/file")
        choice = ask("Pilih opsi", "1")

        if choice == "1":
            folders = sorted([p for p in cwd.iterdir() if p.is_dir()], key=lambda p: p.name.lower())
            # Sembunyikan folder internal
            folders = [f for f in folders if not f.name.startswith((".") ) and f.name != "__pycache__"]
            if not folders:
                print("Tidak ada folder di directory ini.")
                continue
            print(f"\nFolder di {cwd.name}/:")
            for idx, folder in enumerate(folders, 1):
                count = sum(1 for _ in folder.rglob("*") if _.is_file() and is_pack_file(_))
                count_str = c_gray(f"({count} file)") if count > 0 else c_gray("(kosong)")
                print(f"  {idx}) {folder.name}/ {count_str}")
            raw = ask("Pilih nomor folder")
            try:
                index = int(raw)
            except (TypeError, ValueError):
                print("Pilihan harus angka.")
                continue
            if index < 1 or index > len(folders):
                print("Pilihan folder tidak valid.")
                continue
            picked = folders[index - 1]
            pick_count = sum(1 for _ in picked.rglob("*") if _.is_file() and is_pack_file(_))
            if pick_count == 0:
                print(c_warn(f"Folder {picked.name}/ tidak ada file addon. Pilih folder lain."))
                continue
            return picked

        if choice == "2":
            return server_dir.resolve()

        if choice == "3":
            manual = ask("Path folder/file addon/template", str(cwd))
            if not manual:
                continue
            path = Path(manual).expanduser().resolve()
            if not path.exists():
                print(f"Path tidak ada: {path}")
                continue
            return path

        print("Pilih 1, 2, atau 3.")


def get_key():
    """Baca satu tombol untuk picker interaktif."""
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
    """Render picker addon interaktif."""
    print("\033[2J\033[H", end="")
    scan_names = ", ".join(d.name for d in search_dirs if d.exists())
    print(f"Addon/template ditemukan: {c_gray(f'({len(candidates)} file)')}")
    print(f"{c_gray(f'Scan: {scan_names}')}")
    for i, path in enumerate(candidates):
        pointer = ">" if i == cursor else " "
        mark = "x" if i in selected else " "
        folder = c_gray(f'({path.parent.name}/)')
        print(f"  {pointer} [{mark}] {i + 1}. {path.name} {folder}")
    print("\n\u2191/\u2193 pilih addon, Space centang/uncentang, Enter install yang dicentang")
    print("a pilih semua, c kosongkan, r refresh, m input manual, q batal")


def choose_archives_keyboard(candidates, search_dirs):
    """Pilih addon dengan tombol panah + Space."""
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
            manual = ask("Path file")
            if manual:
                manual_path = Path(manual).expanduser().resolve()
                if not manual_path.exists():
                    print(f"File tidak ada: {manual_path}")
                    input("Tekan Enter untuk lanjut...")
                    continue
                if not manual_path.is_file() or not is_pack_file(manual_path):
                    print(f"File bukan addon/template/archive Bedrock: {manual_path}")
                    input("Tekan Enter untuk lanjut...")
                    continue
                candidates.append(manual_path)
                selected.add(len(candidates) - 1)
                cursor = len(candidates) - 1
        elif key == "q":
            print()
            return []


def choose_archives_text(candidates, search_dirs):
    """Fallback picker addon berbasis input teks."""
    selected = set()
    while True:
        print(f"\nAddon/template ditemukan: ({len(candidates)} file)")
        for i, path in enumerate(candidates, 1):
            mark = "x" if i in selected else " "
            folder = f'({path.parent.name}/)'
            print(f"  [{mark}] {i}. {path.name} {folder}")
        print("\nKetik nomor untuk centang/uncentang. Contoh: 1 atau 1,3")
        print("  a. Pilih semua")
        print("  c. Kosongkan pilihan")
        print("  r. Refresh (scan ulang folder)")
        print("  0. Input path manual file")
        print("  kosong. Install yang dicentang")

        choice = ask("Pilih addon/template", "")
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
            print(f"Refreshed! {len(candidates)} file ditemukan.")
            continue
        if choice == "0":
            manual = ask("Path file")
            if manual:
                manual_path = Path(manual).expanduser().resolve()
                if not manual_path.exists():
                    print(f"File tidak ada: {manual_path}")
                    continue
                if not manual_path.is_file() or not is_pack_file(manual_path):
                    print(f"File bukan addon/template/archive Bedrock: {manual_path}")
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
            print("Pilihan tidak valid. Pakai nomor, contoh: 1 atau 1,3")

    return [candidates[i - 1] for i in sorted(selected)]


def choose_archives(server_dir):
    while True:
        source = choose_archive_location(server_dir)
        if source.is_file():
            if not is_pack_file(source):
                print(f"File bukan addon/template/archive Bedrock: {source}")
                continue
            return [source]

        search_dirs = [source]
        candidates = list_archives(search_dirs)
        if candidates:
            break
        print(f"\nTidak ada .mcpack/.mcaddon/.mctemplate/.zip/.tar.gz di: {source.name}/")
        print("Pilih lokasi lain.")

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
        msg = f"Skip {pack_dir}: bukan RP/BP Bedrock"
        print(msg)
        log.warning(msg)
        return []

    header = manifest.get("header", {})
    pack_id = header.get("uuid")
    # Validasi UUID format sebelum dipakai
    validate_uuid(pack_id, context=str(pack_dir))
    version = version_array(header.get("version"))
    log.info("Pack ditemukan: %s | kind=%s | uuid=%s | version=%s",
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
    world_name = safe_world_name(ask(f"Nama world import dari template {src_world.name}", default_name))
    dest = safe_child_path(worlds_dir, world_name, world_name)
    safe_copytree(src_world, dest)
    return dest


def load_manifests_from_archive(archive: Path):
    """Baca manifest.json langsung dari archive tanpa extract penuh."""
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
    """Simulasi install pack dari manifest tanpa extract archive penuh."""
    installed = []
    header = manifest.get("header", {})
    pack_id = header.get("uuid")
    version = version_array(header.get("version"))
    validate_uuid(pack_id, f"manifest {manifest_name} header.uuid")
    kinds = detect_pack_kinds(manifest)
    if not kinds:
        print(f"Lewati manifest tanpa module resource/data: {manifest_name}")
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
    # Validasi ukuran & integritas archive sebelum diekstrak
    validate_archive(archive)
    size_mb = archive.stat().st_size / (1024 * 1024)

    if DRY_RUN:
        print(f"         {c_yellow('[DRY-RUN] Baca manifest tanpa extract penuh')}")
        installed = []
        for manifest_name, manifest in load_manifests_from_archive(archive):
            installed.extend(dry_run_install_manifest(manifest_name, manifest, server_dir))
        return installed, []

    # Step: Ekstrak
    print(f"         {c_info(f'Mengekstrak ({size_mb:.1f} MB)...')}")
    tmp = extract_archive_to_temp(archive)
    installed = []
    imported_worlds = []
    try:
        # Step: Scan isi
        manifests, world_dirs = scan_addon_content(tmp)
        pack_count = len(manifests)
        print(f"         {c_ok(f'{pack_count} pack ditemukan')}")

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
            if yes_no(f"Template/world terdeteksi: {world_dir.name}. Import world?", True):
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
        print(f"JSON rusak: {path}. Akan ditulis ulang list kosong + pack baru.")
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
    # Gunakan newline konsisten (\n) agar kompatibel di semua platform
    write_text(props, "\n".join(new_lines) + "\n")


def choose_world(server_dir, imported_worlds):
    if imported_worlds:
        print("\nWorld hasil import:")
        for i, w in enumerate(imported_worlds, 1):
            print(f"  {i}. {w.name}")
        if yes_no("Pakai world import ini untuk enable pack?", True):
            if len(imported_worlds) == 1:
                return imported_worlds[0]
            # Perbaikan: tangani input non-integer agar tidak crash
            while True:
                raw = ask("Pilih world import", "1")
                try:
                    idx = int(raw)
                    if 1 <= idx <= len(imported_worlds):
                        return imported_worlds[idx - 1]
                    print(f"Pilih angka 1-{len(imported_worlds)}.")
                except ValueError:
                    print("Masukkan angka yang valid.")

    worlds_dir = server_dir / "worlds"
    if not DRY_RUN:
        worlds_dir.mkdir(parents=True, exist_ok=True)
    else:
        action(f"Ensure dir: {worlds_dir}")
    existing = sorted([p for p in worlds_dir.iterdir() if p.is_dir()]) if worlds_dir.exists() else []

    if existing:
        print("\nWorld folder ditemukan:")
        for i, w in enumerate(existing, 1):
            print(f"  {i}. {w.name}")
        print("  0. Buat/pakai nama lain")
        # Perbaikan: tangani input non-integer agar tidak crash
        while True:
            choice = ask("Pilih world", "1")
            try:
                idx = int(choice)
                if idx == 0:
                    break
                if 1 <= idx <= len(existing):
                    return existing[idx - 1]
                print(f"Pilih angka 0-{len(existing)}.")
            except ValueError:
                print("Masukkan angka yang valid.")

    prop_name = read_server_level_name(server_dir)
    default_name = prop_name or "Bedrock level"

    print(c_warn("Belum ada world folder di server ini."))
    print(f"Nama default dari server.properties: {c_bold(default_name)}")

    if yes_no(f"Buat world dengan nama \"{default_name}\"?", True):
        world_name = safe_world_name(default_name)
    else:
        world_name = safe_world_name(ask("Masukkan nama world", default_name))

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
    """Simbol centang/silang dengan warna jika tersedia."""
    if _COLOR_ENABLED:
        return c_green("[\u2713]") if cond else c_gray("[ ]")
    return "[✓]" if cond else "[ ]"


def print_summary(archive_results, dep_missing) -> None:
    """Cetak summary terpisah per addon/archive."""
    for archive_name, packs, worlds in archive_results:
        print(f"\n{c_bold('== Summary ==')} {c_cyan(archive_name)}")

        # Tampilkan nama addon yang dikerjakan beserta UUID
        seen = set()
        for p in packs:
            pid = p["pack_id"]
            if pid not in seen:
                seen.add(pid)
                print(f"{c_ok(p['name'])} {c_gray('(' + pid + ')')}")

        # Tampilkan Dry-run hanya jika di-enable (DRY_RUN = True)
        if DRY_RUN:
            print("Dry-run      : Yes")

        # BP Installed
        bp_packs = [x for x in packs if x["kind"] == "bp"]
        has_bp = len(bp_packs) > 0
        print(f"BP installed : {_tick(has_bp)}")
        if has_bp:
            for p in bp_packs:
                print(f"  location   : {p['path']}")
                print(f"  version    : {'.'.join(map(str, p['version']))}")

        # RP Installed
        rp_packs = [x for x in packs if x["kind"] == "rp"]
        has_rp = len(rp_packs) > 0
        print(f"RP installed : {_tick(has_rp)}")
        if has_rp:
            for p in rp_packs:
                print(f"  location   : {p['path']}")
                print(f"  version    : {'.'.join(map(str, p['version']))}")

        # World Import
        print(f"World import : {_tick(bool(worlds))}")
        for w in worlds:
            print(f"  location   : {w}")

        # Missing Deps per addon
        pack_ids = {p["pack_id"] for p in packs}
        local_missing = [(p, d) for p, d in dep_missing if p["pack_id"] in pack_ids]
        if local_missing:
            print(f"Missing deps : {len(local_missing)} item")
            for pack, dep in local_missing:
                print(f"  {c_err(pack['name'])} perlu {c_yellow(dep['uuid'])} versi {dep.get('version')}")
        else:
            print("Missing deps : Nothing")


# Prefix/pattern folder bawaan Minecraft yang TIDAK boleh dihapus
_BUILTIN_PREFIXES = (
    "vanilla", "chemistry", "editor", "experimental_",
    "server_editor_library", "server_library", "server_ui_library",
)


def _is_builtin_pack(folder_name: str) -> bool:
    """Cek apakah folder ini adalah pack bawaan Minecraft server."""
    name_lower = folder_name.lower()
    # Skip folder backup (.bak-)
    if ".bak-" in name_lower:
        return True
    return any(name_lower == prefix or name_lower.startswith(prefix)
               for prefix in _BUILTIN_PREFIXES)


def get_installed_addons(server_dir):
    """Scan addon yang diinstall user saja (skip bawaan Minecraft & backup)."""
    installed = []
    for kind, folder_name in [("rp", "resource_packs"), ("bp", "behavior_packs")]:
        base = server_dir / folder_name
        if not base.exists():
            continue
        for pack_dir in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not pack_dir.is_dir():
                continue
            if _is_builtin_pack(pack_dir.name):
                log.debug("Skip bawaan/backup: %s", pack_dir.name)
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
                log.warning("Gagal baca %s: %s", manifest_path, e)
    return installed

def render_checkbox_picker_addons(candidates, selected, cursor):
    print("\033[2J\033[H", end="")
    print("Addon terinstall ditemukan:")
    for i, p in enumerate(candidates):
        pointer = ">" if i == cursor else " "
        mark = "x" if i in selected else " "
        kind_str = "RP" if p["kind"] == "rp" else "BP"
        print(f"  {pointer} [{mark}] {i + 1}. [{kind_str}] {p['name']} ({p['path'].name})")
    print("\n↑/↓ pilih addon, Space centang/uncentang, Enter hapus yang dicentang")
    print("a pilih semua, c kosongkan, q batal")

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
        print("\nAddon terinstall:")
        for i, p in enumerate(candidates, 1):
            mark = "x" if i in selected else " "
            kind_str = "RP" if p["kind"] == "rp" else "BP"
            print(f"  [{mark}] {i}. [{kind_str}] {p['name']} ({p['path'].name})")
        print("\nKetik nomor untuk centang/uncentang (contoh: 1 atau 1,3). a=semua, c=kosong, kosong=Lanjut")
        
        choice = ask("Pilih addon yang mau dihapus", "")
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
            print("Pilihan tidak valid.")
    
    return [candidates[i - 1] for i in sorted(selected)]

def disable_pack_in_world(world_dir, pack):
    """Hapus pack dari world JSON config (silent, hanya log ke file)."""
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
    candidates = get_installed_addons(server_dir)
    if not candidates:
        print(c_warn("Tidak ada addon yang terinstall."))
        return
    
    if sys.stdin.isatty():
        to_remove = choose_addons_keyboard(candidates)
    else:
        to_remove = choose_addons_text(candidates)
        
    if not to_remove:
        print("Dibatalkan, tidak ada yang dihapus.")
        return
    
    # Konfirmasi sebelum hapus - PERINGATAN KERAS
    print(f"\n{c_red('!' * 50)}")
    print(f"{c_bold(c_red('  ⚠  PERINGATAN: PENGHAPUSAN PERMANEN  ⚠'))}")
    print(f"{c_red('!' * 50)}")
    print(f"\n{c_bold('Addon yang akan dihapus:')}")
    for i, pack in enumerate(to_remove, 1):
        kind_str = c_cyan("RP") if pack["kind"] == "rp" else c_cyan("BP")
        print(f"  {c_red('✗')} {i}. [{kind_str}] {c_bold(pack['name'])}")
    print(f"\n{c_red('File addon akan DIHAPUS PERMANEN dan TIDAK BISA dikembalikan!')}")
    
    confirm = ask(f"\nKetik {c_bold('HAPUS')} untuk konfirmasi, atau tekan Enter untuk batal")
    if confirm != "HAPUS":
        print("Dibatalkan.")
        return

    worlds_dir = server_dir / "worlds"
    existing_worlds = sorted([p for p in worlds_dir.iterdir() if p.is_dir()]) if worlds_dir.exists() else []
    
    total = len(to_remove)
    removed_names = []
    
    print(c_divider(f"Menghapus {total} addon"))
    for idx, pack in enumerate(to_remove, 1):
        pack_path = pack["path"]
        kind_label = "RP" if pack["kind"] == "rp" else "BP"
        
        print(f"\n  [{idx}/{total}] {c_bold(pack['name'])} ({kind_label})")
        
        # Hapus folder permanen
        if not DRY_RUN and pack_path.exists():
            log.info("Hapus permanen: %s", pack_path)
            shutil.rmtree(pack_path)
            print(f"         {c_ok('Folder dihapus')}")
        elif DRY_RUN:
            print(f"         {c_yellow('[DRY-RUN] Akan dihapus')}")
        
        # Hapus dari world config
        cleaned_worlds = 0
        for w in existing_worlds:
            if disable_pack_in_world(w, pack):
                cleaned_worlds += 1
        if cleaned_worlds > 0:
            print(f"         {c_ok(f'Dicopot dari {cleaned_worlds} world')}")
        
        removed_names.append(f"[{kind_label}] {pack['name']}")
    
    # Summary akhir
    print(c_divider("Uninstall Selesai"))
    print(f"  Total dihapus: {c_green(str(total))} addon")
    for name in removed_names:
        print(f"  {c_ok(name)}")
    print(c_divider())
    print(f"  {c_green('\U0001F680 Restart bedrock_server untuk menerapkan perubahan.')}")
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
        log.info("Mode DRY-RUN aktif")

    print(c_divider("⛏  Bedrock Addon Installer by @zoxervy"))
    print(c_gray(f"Log: {log_file.resolve()}"))
    if DRY_RUN:
        print(c_yellow("Mode: DRY-RUN aktif, tidak ada file yang ditulis."))

    # Step 1: Pilih server
    print(c_divider("Step 1: Pilih Server"))
    server_dir = choose_server_dir()
    print(f"{c_ok(f'Server: {server_dir.name}/')}")

    # Step 2: Pilih aksi
    print(c_divider("Step 2: Pilih Aksi"))
    print("  1) \U0001F4E5 Install Addon")
    print("  2) \U0001F5D1  Uninstall Addon")
    while True:
        action_choice = ask("Pilih aksi", "1")
        if action_choice in ("1", "2"):
            break
        print("Pilih 1 atau 2.")

    if action_choice == "2":
        print(c_divider("Uninstall Addon"))
        uninstall_addon_flow(server_dir)
        return

    # Step 3: Pilih addon
    print(c_divider("Step 3: Pilih Addon"))
    archives = choose_archives(server_dir)
    if not archives:
        print(c_warn("Tidak ada file dipilih."))
        return
    print(f"{c_ok(f'{len(archives)} addon dipilih')}")

    installed = []
    imported_worlds = []
    archive_results = []  # track per-archive results untuk summary
    total_archives = len(archives)

    # Step 4: Proses install
    print(c_divider(f"Step 4: Install ({total_archives} addon)"))
    for idx, archive in enumerate(archives, 1):
        size_str = f"{archive.stat().st_size / (1024*1024):.1f} MB"
        print(f"\n  [{idx}/{total_archives}] {c_bold(archive.name)} {c_gray(f'({size_str})')}")
        packs, worlds = process_archive(archive, server_dir)
        installed.extend(packs)
        imported_worlds.extend(worlds)
        archive_results.append((archive.name, packs, worlds))

    if not installed:
        print(f"\n{c_warn('Tidak ada RP/BP dipasang.')}")
        return

    # Step 5: Pilih world target
    print(c_divider("Step 5: Pilih World"))
    world_dir = choose_world(server_dir, imported_worlds)
    print(f"{c_ok(f'World: {world_dir.name}')}")

    # Step 6: Aktifkan pack di world
    print(c_divider("Step 6: Aktifkan Pack"))
    for pack in installed:
        kind_label = "RP" if pack["kind"] == "rp" else "BP"
        json_file = "world_resource_packs.json" if pack["kind"] == "rp" else "world_behavior_packs.json"
        enable_pack(world_dir, pack)
        pack_name = pack["name"]
        print(f"  {c_ok(f'[{kind_label}] {pack_name}')} {c_gray('→ ' + json_file)}")

    if any(pack["kind"] == "rp" for pack in installed):
        if not check_texturepack_required(server_dir):
            if yes_no("\nSet texturepack-required=true biar client otomatis download pack?", True):
                set_texturepack_required(server_dir)
                print(f"  {c_ok('texturepack-required=true')}")

    # Step 7: Summary
    dep_missing = check_dependencies(installed)
    print_summary(archive_results, dep_missing)
    print(c_divider())
    print(f"  {c_green('\U0001F680 Restart bedrock_server untuk menerapkan perubahan.')}")
    print(f"  {c_gray('Backup file (.bak-*) tetap tersimpan jika ingin di-restore.')}")
    print()



if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{c_yellow('Dibatalkan.')}")
    except Exception as e:
        print(c_err(f"Error: {e}") if _COLOR_ENABLED else f"Error: {e}")
        log.exception("Fatal error")
        raise SystemExit(1)

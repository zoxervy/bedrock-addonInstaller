import re

PACK_EXTS = {".mcpack", ".mcaddon", ".mctemplate", ".zip"}
TAR_EXTS = {".tar.gz", ".tgz", ".tar.bz2"}      # supported tar formats
WORLD_MARKERS = {"level.dat", "levelname.txt"}
MAX_ARCHIVE_MB = 500
UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE
)

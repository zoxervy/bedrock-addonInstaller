# Bedrock Addon Installer

Interactive Python CLI for installing and uninstalling Minecraft Bedrock Server addons.

It supports resource packs, behavior packs, combined addons, nested addon archives, world/template imports, dry-run previews, and safe archive extraction.

## Project files

```text
bedrock-addonInstaller/
├─ app.py       # standalone installer script; copy this file to use elsewhere
└─ README.md    # usage guide and technical notes
```

Runtime is single-file: copy `app.py` by itself if you want to run it from another folder or server.

## Requirements

- Python 3
- A valid Minecraft Bedrock Server folder containing:
  - `server.properties`
  - `bedrock_server.exe` on Windows, or `bedrock_server` on Linux/macOS

Check Python:

```bash
python --version
```

If `python` is not available on Windows, try:

```bash
py --version
```

## Quick start

Run from this folder:

```bash
python app.py
```

Or on Windows:

```bash
py app.py
```

## Inspect addon

Inspect an addon archive without installing it or choosing a server:

```bash
python app.py --inspect path/to/addon.mcaddon
```

Inspect mode:

- does not extract, copy, or write files
- reads `manifest.json` directly from the archive when possible
- scans nested addon archives such as `.mcaddon` files that contain `.mcpack` files
- prints detected BP/RP type, pack UUID, version, manifest source, and dependencies

Use this to check unknown addons before dry-run or install.

## Dry-run mode

Preview actions without writing files:

```bash
python app.py --dry-run
```

Dry-run mode:

- does not fully extract archives
- reads `manifest.json` directly from the archive when possible
- scans nested addon archives such as `.mcaddon` files that contain `.mcpack` files
- simulates destination paths
- avoids writes, copies, and config updates

Use this before installing unknown or large addons.

## Supported addon formats

The installer accepts:

- `.mcpack`
- `.mcaddon`
- `.mctemplate`
- `.zip`
- `.tar.gz`
- `.tgz`
- `.tar.bz2`

The default archive size limit is 500 MB.

## Basic install flow

1. Put addon files in `.addons/` or another known folder.
2. Run:

   ```bash
   python app.py
   ```

3. Choose the Bedrock server folder:
   - `1` use current folder
   - `2` list subfolders
   - `3` enter manual path
   - `0` exit

4. Choose action:
   - `1` install addon
   - `2` uninstall addon
   - `3` reorder world addons
   - `0` exit

5. Choose addon source:
   - local folder
   - server folder
   - manual folder path
   - manual file path
   - `0` exit

6. Select addons.
7. Review the conflict scanner report.
8. Choose target world.
9. Restart `bedrock_server` after installation.

## Cancel controls

Most prompts support:

- `Ctrl+C` cancel current flow
- `0` exit where shown
- `q`, `quit`, `exit`, `cancel`, or `x` cancel where text input is accepted

If install is cancelled after files were copied, the installer rolls back copied packs, imported worlds, and config writes.

## Conflict scanner

Before copying packs, the installer pre-scans selected archives and reports possible conflicts:

- same pack UUID already installed
- selected version differs from installed version
- same pack UUID selected more than once in the batch
- two selected packs planning the same destination folder
- destination pack folder already exists
- required dependency UUID/version mismatch

If conflicts are found, choose:

```text
1) Continue
0) Cancel install
```

Continuing keeps the existing safety prompts before any replacement happens.

## Interactive addon picker

Keyboard mode:

- Up/down arrows: move cursor
- Space: toggle selection
- Enter: confirm selected addons
- `a`: select all
- `c`: clear selection
- `r`: refresh scan
- `m`: add manual file path
- `q`: cancel

Text fallback mode supports:

- `1`
- `1,3`
- `a`
- `c`
- `r`
- `0` for manual file path
- empty input to continue

This fallback is useful when the terminal does not support raw keyboard input.

## Uninstall flow

Run:

```bash
python app.py
```

Then choose:

```text
2) Uninstall Addon
```

The script scans:

- `resource_packs/`
- `behavior_packs/`

It skips:

- built-in Minecraft packs
- backup folders containing `.bak-`
- folders without `manifest.json`

Before removing selected packs, the script requires this confirmation text:

```text
DELETE
```

By default, uninstall moves addon folders to centralized backups and removes pack references from world JSON files:

```text
.temp-addonInstaller/backups/<server-name>/bp/
.temp-addonInstaller/backups/<server-name>/rp/
```

The uninstall output prints the full backup path for each removed addon.
Use `--force-delete` to permanently delete addon folders instead.

## Reorder flow

Run:

```bash
python app.py
```

Then choose:

```text
3) Reorder world addons
```

The script lets you choose a world, then choose either behavior packs or resource packs.

Keyboard mode:

- Up/down arrows: move cursor
- Space: select packs
- Up/down arrows while packs are selected: move selected packs
- Enter: save order
- Confirm commit after saving, or reject/cancel to restore the previous order
- `a`: select all
- `c`: clear selection
- `q`: cancel

Text fallback mode supports:

- `1`
- `1,3`
- `u` move selected packs up
- `d` move selected packs down
- `a`
- `c`
- `q`
- empty input to save

## Files modified by install

Before these writes, the conflict scanner warns about duplicate UUIDs, version changes, duplicate selected packs, duplicate planned destination folders, existing destination folders, and dependency version mismatches.

The script may create or modify:

```text
resource_packs/
behavior_packs/
worlds/<world>/world_resource_packs.json
worlds/<world>/world_behavior_packs.json
server.properties
addonInstaller.log
.temp-addonInstaller/
```

In `--dry-run` mode, `addonInstaller.log` and `.temp-addonInstaller/` are not written.

If existing files or folders are replaced or removed, backups use this suffix:

```text
.bak-YYYYMMDD-HHMMSS
```

If install is cancelled or fails after files have already been copied, the installer rolls back copied packs, imported worlds, and config writes.

## Technical overview

Main flow:

1. Parse CLI arguments.
2. Enable logging and terminal colors.
3. Ask the user to choose a Bedrock server folder.
4. Ask whether to install or uninstall addons.
5. If installing:
   - choose addon source location
   - scan supported archives
   - allow multi-select
   - pre-scan conflicts before copying files
   - validate each archive
   - extract archive safely
   - process nested archives
   - detect RP/BP manifests
   - copy packs into the server
   - optionally import detected worlds/templates
   - choose target world
   - enable installed packs in world JSON
   - optionally set `texturepack-required=true`
6. If uninstalling:
   - scan installed user packs
   - skip built-in packs and backups
   - allow multi-select
   - remove pack references from worlds
   - delete selected pack folders after confirmation

Entrypoint:

```python
if __name__ == "__main__":
    main()
```

## Why the installer is flexible

The script does not require one fixed folder layout.

It can run from:

```text
project/
├─ app.py
├─ .addons/
│  ├─ addon-one.mcaddon
│  └─ addon-two.mcpack
└─ bedrock-server/
   ├─ bedrock_server.exe
   └─ server.properties
```

Or directly inside a Bedrock server folder:

```text
bedrock-server/
├─ app.py
├─ bedrock_server.exe
├─ server.properties
└─ incoming-addons/
   └─ pack.zip
```

It allows manual server paths, manual addon folder paths, and manual single-file paths.

## Server validation

A folder is treated as a Bedrock server only if it contains:

- `server.properties`
- `bedrock_server.exe` on Windows
- `bedrock_server` on Linux/macOS

Validation happens before install or uninstall actions continue. After a server is selected, the installer shows server info including folder, binary name, detected Bedrock server version when available, and configured level name.

## Archive validation

Before processing an archive, the script checks:

- file exists
- file size is under the configured limit
- ZIP archive integrity
- TAR archive integrity

Invalid archives are skipped with a warning.

## Safe extraction

The installer protects against archive path traversal.

For ZIP files:

- `/` and Windows-style `\` member separators are normalized consistently for inspect, dry-run, and extraction
- extraction target must stay inside the temp folder

For localized pack names, `texts/languages.json` entries are accepted only as plain language identifiers. Path-like entries are ignored so a pack cannot make the installer read `.lang` files outside its own `texts/` folder.

For TAR files:

- every target path is checked
- symlinks and hardlinks are blocked

This blocks malicious archive entries such as:

```text
../../server.properties
```

## Nested archive support

Some `.mcaddon` files contain multiple `.mcpack` files inside.

The script handles this by:

1. extracting the outer archive
2. scanning for nested archive files
3. extracting nested archives into `_extracted_*` folders
4. deleting nested archives after successful extraction
5. repeating until no more nested archives are found or max depth is reached

Default nested archive depth is 10.

This allows the installer to handle both simple packs and bundled addon packs.

## Manifest scanning

After extraction, the script scans for:

- `manifest.json`
- `level.dat`
- `levelname.txt`

`manifest.json` identifies resource/behavior packs.

`level.dat` and `levelname.txt` identify world/template folders.

## Resource pack vs behavior pack detection

Pack type is detected from manifest modules:

- module type `resources` means Resource Pack
- module type `data` or `script` means Behavior Pack

One manifest can produce more than one install target if it contains multiple supported module types.

## Pack install behavior

For each detected pack:

1. Validate `header.uuid`.
2. Normalize `header.version` into a Bedrock-compatible version array.
3. Detect RP/BP kind.
4. Choose destination:
   - RP -> `resource_packs/`
   - BP -> `behavior_packs/`
5. Copy the pack folder into the server.
6. Return installed metadata for world activation.

Installed metadata shape:

```json
{
  "pack_id": "uuid",
  "version": [1, 0, 0],
  "path": "...",
  "name": "Pack Name",
  "kind": "rp",
  "dependencies": []
}
```

## World/template import

If extracted content looks like a world/template, the script asks whether to import it.

A folder is treated as a world when it contains:

- `level.dat`
- `levelname.txt`

On import:

1. Read `levelname.txt` if available.
2. Choose whether to create a new independent world or replace an existing world.
3. For a new world, ask for a new folder name and refuse names that already exist.
4. For replacement, warn that the existing world progress will be overwritten and create a backup first.
5. After creating a new world, ask whether to update `server.properties` `level-name` to that new world or leave it for manual setup.
6. Show imported world action, backup, and `server.properties` status in the final summary.

## World pack activation

After installing packs, the script asks for a target world.

Then it updates:

- `world_resource_packs.json` for resource packs
- `world_behavior_packs.json` for behavior packs

Before adding a pack, it removes any existing entry with the same `pack_id`.

This avoids duplicate entries while allowing upgrades or reinstalls.

Written entry format:

```json
{
  "pack_id": "uuid",
  "version": [1, 0, 0]
}
```

## Texture pack auto-download

If any resource pack is installed, the script checks `server.properties`.

If `texturepack-required=true` is not already set, it asks whether to enable it.

When enabled, Bedrock clients are forced to download the resource pack when joining.

## Logging

Normal mode writes logs to:

```text
addonInstaller.log
```

Dry-run mode disables file logging to avoid writes.

The log records:

- archive validation
- extraction
- pack detection
- skipped folders
- warnings
- fatal errors

## Strengths

- Supports many archive formats.
- Handles bundled/nested addon archives.
- Works from multiple folder layouts.
- Has dry-run support.
- Has keyboard and text selection modes.
- Protects against ZIP/TAR path traversal.
- Blocks unsafe TAR links.
- Validates UUID format.
- Backs up existing files before replacement.
- Warns about install conflicts before copying files.
- Avoids duplicate world pack entries.
- Can import worlds/templates.
- Can uninstall user-installed packs.

## Current limitations

- CLI options are available for dry-run and force-delete uninstall behavior.
- Normal uninstall stores backups under `.temp-addonInstaller/backups/<server-name>/bp|rp/`.
- Most behavior is interactive, not fully scriptable.
- Server folder must contain the Bedrock binary and `server.properties`.
- Archive size limit is hardcoded through `MAX_ARCHIVE_MB`.
- Pack dependency handling reports missing dependencies but does not automatically resolve them.
- Uninstall detection depends on installed pack folders containing `manifest.json`.

## Safe usage checklist

Before installing:

1. Put addon files in a known folder, preferably `.addons/`.
2. Run:

   ```bash
   python app.py --dry-run
   ```

3. Confirm the detected RP/BP names.
4. Confirm the selected server folder.
5. Install normally:

   ```bash
   python app.py
   ```

6. Backup important worlds manually if the server is production.
7. Restart `bedrock_server` after install.

## Troubleshooting

### Server folder is rejected

Make sure the folder contains:

- `server.properties`
- `bedrock_server.exe` or `bedrock_server`

### Addon does not appear

Check that the addon file is inside `.addons/` or choose a manual file path.

Also confirm the file extension is supported.

### Client does not download resource pack

When asked:

```text
Set texturepack-required=true so clients automatically download the pack?
```

Choose `y`.

### World JSON is corrupted

The script creates a backup, then rewrites the JSON list.

Look for backup files ending with:

```text
.bak-YYYYMMDD-HHMMSS
```

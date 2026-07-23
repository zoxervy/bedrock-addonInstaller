# Bedrock Addon Installer — Project State / Handoff

**Updated:** 2026-07-23  
**Branch:** `main`  
**Project type:** Standalone Python 3 CLI, standard library only  
**Primary implementation:** [`app.py`](app.py)

This document records the current project state for another developer taking over the work.

---

## 1. Current repository status

### Working tree

The latest completed changes are **not committed or pushed yet**.

Expected working-tree changes:

```text
M  app.py
M  README.md
?? test_batch1.py
?? __pycache__/
```

Notes:

- `__pycache__/` is generated Python cache and should **not** be committed.
- `test_batch1.py` is a new stdlib `unittest` fixture suite created for the first audit-fix batch.
- The branch includes the prior UI/polish history through commit `2d3edf2 Simplify world template scan output`.

### Last committed history

```text
2d3edf2 Simplify world template scan output
25a9d9b Clarify pack summaries and imported world sources
54eedd9 Use addon filenames for readable pack names
48056c2 Polish addon installer UI and template handling
```

---

## 2. What the app currently does

The CLI supports:

- Installing Minecraft Bedrock **Behavior Packs (BP)** and **Resource Packs (RP)**.
- Combined BP/RP addons.
- `.mcpack`, `.mcaddon`, `.mctemplate`, `.zip`, `.tar.gz`, `.tgz`, and `.tar.bz2` archives.
- Nested addon archives.
- Safe ZIP/TAR extraction with traversal protections.
- `--dry-run` preview mode.
- `--inspect PATH` archive manifest inspection without server selection.
- Pack activation through:
  - `world_behavior_packs.json`
  - `world_resource_packs.json`
- World import when actual world markers are found (`level.dat` / `levelname.txt`).
- Reordering world addon entries.
- Uninstall with centralized backups by default; `--force-delete` permanently deletes pack folders.

---

## 3. Important user-facing behavior already implemented

These behaviors were requested previously and must be preserved in future changes.

### Server information

After choosing a server, the app shows:

- Selected folder
- Bedrock server binary
- Best-effort detected server version
- Current `level-name` when available

Relevant helpers:

```python
server_binary_name()
server_binary_path()
detect_server_version()
format_server_version()
print_server_info()
```

### Addon picker selection order

Addon selection uses ordered lists, not sets:

- The first checked archive is install order `1`.
- The picker displays the selected order beside checked files.
- Keyboard and text picker both preserve selection order.

Relevant helpers:

```python
ui_checkbox_row()
choose_archives_keyboard()
choose_archives_text()
```

### Readable installed folder names

Pack destination names prefer the archive/source title, then use manifest version and pack kind.

Examples:

```text
WAILA.v5.1.1.mcaddon
→ WAILA v5.1.1 BP
→ WAILA v5.1.1 RP
```

Relevant helpers:

```python
strip_bedrock_formatting()
clean_pack_title()
source_title()
title_for_pack()
manifest_version_label()
pack_folder_name()
```

Minecraft formatting codes (`§l`, `§6`, etc.), bracketed BP/RP tags, version decorations, and manifest translation-comment noise are cleaned for display/folder naming.

### Backup layout

Backups are centralized:

```text
.temp-addonInstaller/backups/<server-name>/<category>/
```

Typical categories:

```text
bp/
rp/
worlds/
config/
other/
```

Relevant helpers:

```python
backup_root_for_path()
backup_category_for_path()
backup_existing()
uninstall_backup_path()
rollback_path()
rollback_install()
```

### `world_template` behavior

A manifest module with type `world_template` is **metadata only**:

- It is not installed as BP or RP.
- It is intentionally hidden from normal user-facing detected-count output.
- A full world is importable only when the archive contains real world markers such as `level.dat` / `levelname.txt`.

### Imported-world local packs

Packs inside imported worlds can be labeled:

```text
(from imported world)
```

Relevant helpers:

```python
find_world_local_pack_path()
pack_source_label()
source_suffix_for_items()
```

### UI wording and summary display

- Uninstall picker title is `Select addon to uninstall`.
- Summary uses `Behavior Pack` and `Resource Pack` capitalization.
- Included pack labels are green.

---

## 4. Audit Batch 1 — completed but uncommitted

The first safety/transaction batch has been implemented and verified.

### 4.1 ZIP member path normalization

**Problem fixed:** ZIP archives may store paths with Windows separators:

```text
behavior_packs\example\manifest.json
```

Previously, extraction normalized that path but inspect/dry-run used platform-dependent `Path(name)` logic. On Linux/macOS, manifest detection could disagree with actual extraction.

**Current behavior:** ZIP paths are normalized through portable archive semantics before use in:

- ZIP extraction
- `--inspect`
- `--dry-run`
- Nested archive detection
- Virtual pack-folder/destination calculations

Relevant helpers:

```python
normalized_zip_member_name()
zip_member_path()
is_pack_name()
load_manifests_from_zip_file()
_virtual_pack_dir()
safe_extract()
```

### 4.2 `texts/languages.json` path safety

**Problem fixed:** malicious `languages.json` entries could point outside a pack’s `texts/` folder, including traversal and Windows UNC-like paths.

Blocked examples:

```text
../outside
\\host\share
C:drive
/absolute/path
```

**Current behavior:** only plain language identifiers are accepted. Candidate language files are constructed through `safe_child_path()`.

Relevant helpers:

```python
safe_language_code()
language_files_for_pack()
```

### 4.3 Install and world-import transaction tracking

**Problem fixed:** pack/world copy errors could happen after a destination was modified but before the outer rollback list knew about it. Also, `process_archive()` previously caught broad install errors and displayed them as invalid manifests.

**Current behavior:**

- Pack rollback records are registered before destination mutation/copy begins.
- Existing same-UUID pack replacements record the old path and backup before removal.
- World imports/replacements enter transaction tracking before copy begins.
- `server.properties` changes made while importing a new world are registered before write.
- Operational errors (copy, backup, remove, disk space, etc.) are allowed to reach `main()` and trigger `rollback_install()`.
- Invalid manifests are still skipped before filesystem mutations begin.

Relevant helpers/functions:

```python
safe_copytree(..., on_prepared=...)
replace_copytree(..., on_prepared=...)
write_text(..., on_prepared=...)
install_pack_dir(..., transaction_installed=...)
validate_install_manifest()
import_world_as_new(..., transaction_worlds=...)
import_world_replace(..., transaction_worlds=...)
process_archive(...)
rollback_install()
```

### 4.4 Install collision detection

**Problem fixed:** two separate archives with different UUIDs could calculate the same readable destination folder name and collide during the copy phase.

**Current behavior:** `scan_install_conflicts()` reports:

```text
batch_duplicate_dest
```

when multiple selected pack records resolve to the same planned destination path.

Relevant helper:

```python
scan_install_conflicts()
```

### 4.5 Dependency version checking

**Problem fixed:** dependencies were considered available solely because their UUID existed; required version was ignored.

**Current behavior:** dependencies are classified as:

```text
found
version_mismatch
missing
```

A dependency that exists at the wrong version is reported separately in the selected-content and final summary UI.

Relevant helpers:

```python
normalized_dependency_version()
pack_version_index()
dependency_status()
check_dependencies()
build_archive_batch_context()
```

### 4.6 README updates in Batch 1

[`README.md`](README.md) now documents:

- Duplicate planned destination warnings.
- Dependency UUID/version mismatch warnings.
- ZIP `/` and `\` normalization consistency.
- `languages.json` entries restricted to local language identifiers.
- `script` modules also count as Behavior Packs.

---

## 5. Batch 1 verification completed

### Commands run successfully

```bash
python -m py_compile app.py test_batch1.py
```

```bash
python app.py --help
```

```bash
python -m unittest -v test_batch1.py
```

### Test suite

New test file: [`test_batch1.py`](test_batch1.py)

Current test coverage:

1. ZIP manifest uses `\` separators and is still detected.
2. `languages.json` cannot traverse outside `texts/` or access UNC/drive-like paths.
3. Two selected archives that plan the same destination create `batch_duplicate_dest` conflict.
4. Dependency UUID found at the wrong version returns `version_mismatch`.
5. Failed partial BP copy rolls back the new partial destination and restores an old replaced pack folder.
6. Failed partial world copy registers a rollback record and removes the partial imported world.

Last result:

```text
Ran 6 tests
OK
```

---

## 6. Remaining audit work — not implemented yet

These were identified during the full audit and remain pending.

### Batch 2 — Reorder correctness (high priority)

Relevant implementation area:

```python
combined_world_reorder_entries()
split_combined_reorder_entries()
save_combined_world_order()
reorder_addon_flow()
```

Issues still to fix:

1. **Name-based grouping can drop pack entries.**
   - Current group storage uses `items[kind] = item`.
   - Multiple BP or multiple RP packs that normalize to the same display key can overwrite each other.

2. **Unrelated addons can merge.**
   - Grouping is based heavily on cleaned name (`addon_group_key_from_name()`), not stable identity.

3. **No-op reorder save can alter independent BP/RP order.**
   - Combined rows can impose one relative order across both JSON files even when original BP and RP order differed.

4. **Partial reorder writes need stronger transaction protection.**
   - If one JSON write succeeds and the second fails, the first must be restored automatically.

Recommended direction:

- Preserve stable individual item identity using at least `kind + pack_id + version + source/scope`.
- Group BP/RP only with a conservative, safe relationship signal.
- Store lists per kind, never a single `items[kind]` value when duplicates are possible.
- Preserve separate BP/RP order unless a relevant row is actually moved.
- Make dual-file writes rollback immediately on failure.

### Batch 3 — Uninstall correctness and rollback (high priority)

Relevant implementation area:

```python
group_uninstall_candidates()
disable_pack_in_world()
uninstall_addon_flow()
uninstall_backup_path()
```

Issues still to fix:

1. **Uninstall grouping may select unrelated packs.**
   - Same cleaned-name collision can put multiple packs into one UI group.

2. **Uninstall is not transactional yet.**
   - A pack may be moved to backup before a later world JSON write fails.
   - Result can be an active world config referencing a missing folder.

3. **Cleanup matches only `pack_id`.**
   - Multiple versions of a UUID can be removed together.
   - Global uninstall can disable a world-local pack with the same UUID.

4. **World-local packs are incomplete in discovery/uninstall.**
   - They can be labeled in some displays but are not fully first-class uninstall candidates.

Recommended direction:

- Build an uninstall plan before mutation.
- Batch JSON modifications: write each JSON file once and retain backups.
- For normal uninstall, restore both moved folders and config backups when any later action fails.
- Keep `--force-delete` explicit: JSON can roll back, permanently deleted folders cannot.
- Carry `scope` / source data to distinguish server-global from world-local packs.

### Batch 4 — World-local display and builtin detection (medium priority)

Issues remaining:

1. `_is_builtin_pack()` is too broad because prefix checks can hide user-installed folders such as a folder beginning with `vanilla` or `editor`.
2. When a world JSON references a local pack not installed globally, it can still show `Unknown pack <uuid>` instead of reading the local manifest name.
3. World-local scanning should be cached/centralized rather than repeated per displayed row.

Recommended direction:

- Split backup-folder detection from known built-in detection.
- Use exact/constrained built-in names instead of broad prefixes.
- Add a compatible `get_world_local_addons(world_dir)` helper and merge it into status/reorder lookup for the selected world.

### Batch 5 — Documentation alignment (after code behavior stabilizes)

README items still needing follow-up after Batch 2/3 implementation:

- Reorder currently says users choose BP/RP separately, while the current UI combines addon rows.
- Backups need a clearer centralized location statement:

  ```text
  .temp-addonInstaller/backups/<server-name>/<category>/
  ```

- Troubleshooting corrupted world JSON should point to the `config/` backup category.
- Technical overview says uninstall deletes folders; default behavior is actually backup move.
- `world_template` should be documented as metadata-only, with full-world import requiring `level.dat`/`levelname.txt`.
- Document readable destination naming from archive/source title + manifest version + BP/RP.
- Document `--inspect` under CLI options/limitations if not already reflected by surrounding docs.

---

## 7. Suggested next steps for the next developer

1. Review the uncommitted Batch 1 diff:

   ```bash
   git diff -- app.py README.md test_batch1.py
   ```

2. Run the Batch 1 checks:

   ```bash
   python -m py_compile app.py test_batch1.py
   ```

   ```bash
   python -m unittest -v test_batch1.py
   ```

3. If the Batch 1 review is accepted, commit `app.py`, `README.md`, and `test_batch1.py`.
   - Do **not** add `__pycache__/`.

4. Implement **Batch 2 (reorder)** before touching uninstall grouping, because the same grouping/identity model should be reused by Batch 3.

5. Implement **Batch 3 (uninstall transaction)** after stable identity/grouping helpers exist.

6. Re-run the full test suite and manual disposable-server tests after each batch.

---

## 8. Useful commands

### Syntax and unit tests

```bash
python -m py_compile app.py test_batch1.py
```

```bash
python -m unittest -v test_batch1.py
```

### CLI options

```bash
python app.py --help
```

### Archive inspection without server selection

```bash
python app.py --inspect path/to/addon.mcaddon
```

### Interactive no-write preview

```bash
python app.py --dry-run
```

### Review pending changes

```bash
git status --short
```

```bash
git diff --check
```

```bash
git diff -- app.py README.md test_batch1.py
```

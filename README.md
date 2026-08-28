# sorto

**sorto** (file SORTer/Organizer) is a production-quality CLI + TUI that reorganizes files under a directory you choose. It investigates each regular file once, asks a *local* OpenAI-compatible LLM what the file is and where it should live, shows live progress in a terminal UI, then moves/renames it into a sensible destination **under the same root**.

It never deletes, never overwrites, and never rewrites file contents.

This is a **100% AI project**: the code, tests, TUI, and docs were written by AI (Grok Build / Grok 4.6), not by hand.

## Install

Python 3.11+ (developed against 3.11–3.14).

Use a **virtual environment**. On Arch (and Fedora, Debian 12+, …) `python3 -m pip install …` against the system interpreter fails with `externally-managed-environment` (PEP 668). Do not pass `--break-system-packages`; install into a venv / virtualenv instead.

**venv (stdlib, recommended):**

```bash
cd sorto
python3 -m venv .venv
source .venv/bin/activate          # fish: source .venv/bin/activate.fish
pip install -U pip
pip install -e ".[dev]"
sorto --help
```

Without activating, call `.venv/bin/sorto` and `.venv/bin/pip` directly. `make install` does the same (creates `.venv` if needed).

**virtualenv** (if you prefer the `virtualenv` package, Arch: `pacman -S python-virtualenv`):

```bash
cd sorto
virtualenv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

**uv:**

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

**pipx** (isolated *app* install, not an editable checkout):

```bash
pipx install .
# or: pipx install git+ssh://git@github.com/janttsu/sorto.git
```

Optional MIME helper (inside the same virtualenv):

```bash
pip install -e ".[magic]"
# plus system libmagic, e.g. Arch: pacman -S extra/file
```

## Point it at Ollama or LM Studio

sorto talks OpenAI-style chat completions. Defaults:

- base URL: `http://127.0.0.1:11434/v1` (Ollama)
- model: `qwen2.5-coder:14b` (override with `--llm-model` or config)

**Ollama**

```bash
ollama pull qwen2.5-coder:14b
ollama serve   # if not already running
sorto run --root ~/Inbox --llm-model qwen2.5-coder:14b
```

**LM Studio** — start the local server (often `http://127.0.0.1:1234/v1`):

```bash
sorto run --root ~/Inbox --llm-url http://127.0.0.1:1234/v1 --llm-model your-model-id
```

**llama.cpp / vLLM** — any server that implements `/v1/chat/completions` works. API keys are optional; many local servers ignore them (`--llm-api-key` or `SORTO_LLM_API_KEY`).

Check the stack:

```bash
sorto doctor --root ~/Inbox
```

## Quick start

```bash
sorto init --root ~/Inbox
# edit ~/Inbox/_organization/config.toml and prompts/classify.md if you like
sorto run --root ~/Inbox --llm-model qwen2.5-coder:14b
```

Default mode is **continuous**: keep scanning until you quit. New files that appear during the run are picked up.

One-shot (scan current tree, drain the queue, exit):

```bash
sorto run --root ~/Inbox --once --llm-model qwen2.5-coder:14b
```

Dry run / suggestions only (no moves):

```bash
sorto run --root ~/Inbox --dry-run --once
sorto run --root ~/Inbox --suggest-only
```

Headless (no TUI; useful for scripts and CI):

```bash
sorto run --root ~/Inbox --once --no-tui --fake-llm   # fake-llm is a hidden test helper
```

## Safety guarantees

1. **Never delete** user files by default. A move unlinks the source *after* a successful exclusive create at the destination (same inode via rename/hardlink, or a complete copy on cross-device). The only exception is opt-in `--delete-duplicates` (see below), which still never deletes anything inside a git repository.
2. **Never overwrite.** If the destination exists, sorto generates `name-2.ext`, then `name-3.ext`, then a timestamp/hash suffix. Placement uses `renameat2(RENAME_NOREPLACE)` on Linux, then `link`+`unlink`, then `O_CREAT|O_EXCL` copy. `os.replace` is not used on an unproven dest.
3. **Never modify file contents.** Only rename and/or move.
4. **Never leave the root.** `dest_rel` is validated: relative, no `..`, no `_organization/`, filename present.
5. **Never sort its own state.** `<root>/_organization/**` is excluded from scanning and from destinations.
6. **Never extract archives.** Zip/tar/7z/rar stay archives and go under `archives/`.
7. **Uncertain classification** → `_unsorted/` (unless `--dry-run` / `--suggest-only`, which only record a plan).
8. **Crash-safe resume.** A file is `done` only after a durable `progress.jsonl` line is fsync'd following a successful move (or an explicit skip). Interrupted `moving` rows are recovered on the next start.
9. **Duplicates are kept** unless you pass `--delete-duplicates` (or set `delete_duplicates = true` in config). Matching `sha256` of an already-done file is planned under `_duplicates_candidates/` and the original is left intact. With `--delete-duplicates`, the later copy is unlinked **only** after the original is confirmed present — and **never** if the file sits inside a git working tree (a `.git` directory or file in any parent). Dry-run never unlinks. Sampled hashes of huge files are not treated as delete-worthy duplicates.
10. **Compact LLM packets only.** First 2–8 KB preview + metadata, never the whole large file.

## State directory

Created at `<root>/_organization/`:

| path | role |
| --- | --- |
| `config.toml` | per-root settings (LLM, workers, excludes, dest scheme) |
| `progress.jsonl` | append-only durable action log |
| `index.sqlite` | queryable state (TUI source of truth) |
| `sorto.log` | rotating application log |
| `prompts/classify.md` | editable system prompt |
| `cache/` | reserved |

User-wide defaults: `~/.config/sorto/config.toml` (merged under the per-root file; CLI flags win).

## Commands

```
sorto init [--root PATH]
sorto run --root PATH [options]
sorto resume --root PATH
sorto status --root PATH
sorto doctor [--root PATH]
sorto config [--root PATH]
```

### `run` options

| option | meaning |
| --- | --- |
| `--root PATH` | directory to organize (required) |
| `--dry-run` / `--suggest-only` | plan only; do not move |
| `--yes` | apply `needs_user` suggestions instead of holding them |
| `--delete-duplicates` | unlink later copies with the same full sha256 as an already-done file; **never inside a git repo** |
| `--workers N` | parallel LLM workers (default 1) |
| `--scan-interval SEC` | rescan interval (default 5) |
| `--once` | exit when the queue is empty after a full scan |
| `--follow` | keep watching (default) |
| `--include GLOB` | repeatable |
| `--exclude GLOB` | repeatable; `_organization/**` always excluded |
| `--max-file-mb N` | metadata-only above this size (default 64) |
| `--llm-url URL` | OpenAI-compatible base URL |
| `--llm-model NAME` | model id |
| `--llm-api-key KEY` | optional |
| `--dest-scheme default\|by-type\|by-type-year` | how destinations are built |
| `--log-level LEVEL` | `DEBUG` / `INFO` / … |
| `--no-tui` | status lines on stdout instead of Textual |

`sorto resume` is `run` plus retry of `error` rows after a crash.

## TUI keybindings

| key | action |
| --- | --- |
| `q` | quit (waits briefly for in-flight work, then abandons it unfinished) |
| `p` | pause / resume (no new work while paused) |
| `d` | toggle dry-run **only when idle** |
| `o` | open a tail of `progress.jsonl` |
| `?` | help overlay |

The header shows root, model, LIVE/DRY, and whether the scan is live or draining. The progress bar may drop as the walker finds more files; that is expected. ETA is `unknown` until 5 files have completed, then uses a rolling average of identify + analyze + move times.

## Resume after a crash

State lives next to the data, not in memory.

```bash
sorto resume --root ~/Inbox
# equivalent to run, but retries rows left in error
sorto status --root ~/Inbox
```

- `done` files are not re-analyzed unless size/mtime (or hash) changed.
- In-flight `identifying` / `analyzing` rows reset to `discovered`.
- `moving` with source gone and dest present is completed from the log.

## Destination scheme

Top-level folders are created only when needed:

`documents/`, `spreadsheets/`, `presentations/`, `images/photos/`, `images/screenshots/`, `images/diagrams/`, `video/`, `audio/`, `code/`, `archives/`, `data/`, `installers_and_binaries/`, `email_and_exports/`, `3d_and_cad/`, `design/`, `ebooks/`, `_unsorted/`, `_duplicates_candidates/`.

- `default` — LLM proposes `dest_rel` (validated).
- `by-type` — `{mapped-label}/{filename}`.
- `by-type-year` — `{mapped-label}/{year}/{filename}` from mtime.

Renames happen only when the original name looks meaningless (`IMG_1234`, `DSC0001`, `untitled`, `download (3)`, `scan0001`, …). Useful names are preserved. Original extension is kept unless you enable `allow_extension_fix` in config.

## Required / optional system tools

| tool | required? | used for |
| --- | --- | --- |
| Python 3.11+ | yes | runtime |
| A local OpenAI-compatible LLM | yes for real classification | analyze stage |
| `file` | optional | MIME/magic |
| libmagic / `python-magic` | optional | MIME |
| `exiftool` | optional | image metadata |
| `mediainfo`, `ffprobe` | optional | AV metadata |
| `pdfinfo` | optional | PDF metadata |
| `identify` (ImageMagick) | optional | image dimensions |

If the LLM is down, sorto keeps scanning and identifying, shows **blocked** in the TUI, and retries analysis.

## Architecture (short)

- 1 scan thread, N identify workers, `--workers` LLM workers, 1 serial move worker, TUI on the main thread.
- Compact analysis packet per file (path, size, mime, tiny hex/text preview, optional tool snippets, sha256).
- SQLite + JSONL; the TUI never walks the tree or calls the network.

## Development

```bash
make install
make test
make lint
make doctor
make compile   # byte-compile + sdist/wheel in dist/
```

Tests use a fake LLM client and a temp directory fixture: no overwrites, no deletes, collision names, dry-run, resume, rediscovery, `..` rejection, invalid JSON, `_organization` exclusion, and ETA sampling.

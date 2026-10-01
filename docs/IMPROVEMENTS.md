# sorto: review notes and proposals

A review of the whole code base, kept up to date with what has been done
since (version 0.1 beta). Items that are in the code are marked **done**; everything
else is a proposal, roughly ordered by how much it would help.

All numbers were measured on one test machine:

- a Johnny.Decimal archive of about **800,000 files** on btrfs on a hard disk
- a desktop with 31 GB RAM and a 6 GB GPU
- Ollama serving `qwen3.6:35b-a3b` and `qwen3.5:9b`

## Where the time goes

| Stage | Cost per file | Notes |
| --- | --- | --- |
| Scan (walk + stat) | 0.16 ms | 6,400 files/s. A cold `find` of the archive takes 69 s |
| Database insert (first sight) | 0.3 ms | 3,250 files/s, one fsync per insert |
| Identify (mime, text, EXIF, preview, hash) | 0.1–2 s | runs in 4 threads ahead of the model |
| **Model answer, 35B** | **~10 s** | 3.5 s packet + 6.5 s for ~190 answer tokens at 30 tok/s |
| Model answer, 35B, outline not cached | 40–170 s | 6,600 prompt tokens, 165 tok/s idle, ~40 tok/s while the machine is busy (MoE experts on the CPU) |
| Model load, 35B / 9B | 16 s / 15 s | from the page cache |
| **Model answer, 9B fully on the GPU** | **3–4 s** | 47 tok/s |

The model is the bottleneck by two orders of magnitude, but only during
sorting. For reorganizing a large tree, the scan and database also matter.

## 1. Model time (biggest effect)

### 1.1 Keep the prompt cache alive: done, partly

Ollama reuses the KV cache of the longest matching prompt prefix. sorto's
system prompt (instructions + outline + rules) is identical for every file,
so after the first file only the file packet is read. Losing that cache costs
40 s on the 35B when the machine is idle, and was measured at 168 s while
the machine was busy. It is lost when:

- another client uses the same model with a different prompt (Grok Build),
- another model is loaded (the same Ollama holds one model at a time),
- the model unloads after `OLLAMA_KEEP_ALIVE` (30 min in the test setup).

Measured: in the benchmark, one request paid a 71 s reload because another
client had loaded a different model in between.

Done:

- A **warm-up request** at start and after a model switch loads the model
  and fills the cache while sorto is still scanning.
- **Rules and the reorganize note come after the outline.** Editing
  `rules.md` then only re-reads the rules, because Ollama keeps the cached
  prefix up to the first changed token.

Proposals:

- **A dedicated Ollama instance for sorto.** For example
  `ollama serve` on :11437 with `OLLAMA_MAX_LOADED_MODELS=1` and the 9B
  16k tag. Grok Build and sorto then never evict each other's cache. The
  models directory can be shared, and the 9B only needs about 6 GB. Raising
  `OLLAMA_NUM_PARALLEL` to 2 is *not* a good alternative: Ollama multiplies
  the KV cache by the number of slots. 2 × 64k spills to the CPU, measured
  at 12.5 tok/s instead of 24.
- **Use Ollama's native `/api/chat` when the server is Ollama.** It honours
  `keep_alive` per request (the OpenAI endpoint ignores it; tested) and
  `options.num_ctx` / `num_gpu`. sorto could then keep its model loaded for
  as long as a run lasts, without changing the server-wide setting.
- **`OLLAMA_KEEP_ALIVE=-1`** in the user service keeps the model loaded
  forever. The cost is ~22 GB of page cache held for the 35B.

### 1.2 Shorter answers: proposal

About two thirds of the 10 s per file is generation: the JSON answer is
170–200 tokens. The summary (2–3 sentences) is the point of the TUI, but
`reason` often repeats it.

- Limit `reason` to about 12 words and the summary to two sentences.
  Expected: ~110 tokens, about 3 s saved per file on the 35B (−30%).
- Or a `--brief` mode that asks only for `jd_id`, `confidence` and a
  one-line summary, for bulk runs where nobody watches the TUI.

### 1.3 Smaller per-file packet: proposal

The packet sends several fields that do not help the model:

- `sha256` (64 hex chars)
- `hex_preview` (up to 512 hex chars) even when the type is already known
- `mime`, `magic` and `type_guess`, which usually say the same thing three times

Dropping them saves about 150–250 prompt tokens per file, 0.5–1 s on the 35B.

### 1.4 Two models: partly done

The measured 9B (fully on the GPU) is about three times faster than the 35B,
and on six generic test files it chose the same IDs.
The two cannot stay loaded together on a 6 GB GPU (a switch reloads), so the
efficient way to combine them is by pass, not by file:

1. `sorto run … --llm-model qwen3.5:9b-16k` files everything the small
   model is sure about (confidence ≥ 0.7).
2. `sorto resume … --llm-model qwen3.6:35b-a3b` re-asks only the files
   left in the source.

A `--escalate-to MODEL` option could run pass 2 automatically once pass 1
has drained the queue.

Done: decisions that change the archive for good are never left to the small
model. Naming a new ID, choosing its category and inventing a category are
always asked of `structure_model` (the 35B by default), whichever model reads
the files. On a GPU that holds one model at a time this costs two model loads
per new ID, about a minute; files that go to existing IDs are not affected.

### 1.5 Do not ask the model when the answer is already known: partly done

- **Whole folders: done.** A folder is judged once as a whole. A coherent one
  (a trip, an album, a project) moves into its ID as one folder, and its
  files are only checked against the folder's profile (type, EXIF date, GPS,
  camera); a file that stands out is asked about on its own. A 300-photo trip
  is one model call instead of 300. The user's own photos and videos from
  such a folder go to `YYYY/MM` by capture date, also without a model call
  each. Still open: bursts inside a *mixed* folder (same day, a few km apart)
  are asked about one by one.
- **File patterns: done.** `junk.md` lists patterns (`*.dll`, `Thumbs.db`)
  that go straight to one ID without the model.
- **Exact rules.** A rule written as `from:billing@example.com → 13.13`
  (a small optional syntax next to the free-form rules) could be applied
  without the model and shown as "rule" in the TUI. This would also fix a
  measured weakness of free-form rules: both the 9B and the 35B cited
  "Emails from billing@northwind.example are bills" for a *PDF* bill from
  the same company. The filing was right, but the rule's condition did not
  hold. Stricter prompt wording did not change this.
- **A cache that survives outline changes.** Today the cache key contains
  the hash of the whole system prompt, so creating any new folder in the
  target throws away every cached answer. Keying on the file content plus
  the chosen ID, and re-validating that the ID still exists, would keep
  answers across runs. (`--clear-cache` empties the cache and the remembered
  folder decisions on purpose; this proposal is about not losing them by
  accident.)
- **Where a photo was taken: done.** The model is no longer asked to read a
  place out of coordinates. sorto looks the position up in a city list that
  ships with it and tells the model the city and the distance.

### 1.6 Compare two models on the same file: proposal

`m` switches models for the *next* files, so a comparison sees different
files. A `c` key could re-ask the other model about the file just shown and
display both answers side by side: summary, ID, confidence and time. This
costs one reload each way on this GPU, so it is best as an explicit action,
not a mode.

## 2. Scanning

### 2.1 One `lstat` per file: proposal (3× faster walk)

`iter_regular_files` calls `path.is_symlink()` and `path.is_file()`, and
`discover_batch` calls `stat()` again: three system calls per file, plus
`resolve()` for every directory. A `scandir`-based walker that uses the
cached `d_type` and a single `entry.stat(follow_symlinks=False)` walked
the same 340,000 files in **17.5 s instead of 52.9 s**.

### 2.2 Batch database inserts: proposal

Every newly seen file is its own transaction, with `synchronous=FULL` and a
commit (fsync) per statement: 3,250 inserts/s. The first scan of 800,000 files
spends about 4 minutes on inserts alone. Committing every 1,000 files in one
transaction (and `synchronous=NORMAL`, which is safe in WAL mode; the fsync'd
`progress.jsonl` stays the durable record of moves) brings this to seconds.

### 2.3 Compile the glob patterns once: proposal

`should_include` matches every file against every exclude pattern with a
recursive, per-segment `fnmatch`, re-importing `fnmatch` on each call.
Translating the patterns once into a single compiled regex, and pruning
excluded directories before descending (partly done), removes most of this.

### 2.4 Follow mode without a full rewalk: proposal

With `--follow` the whole source is walked again every 5 s. For a
Downloads folder that is fine; for a big source it is not. Options:

- inotify (`watchdog`), which reports new files in real time,
- or remember each directory's mtime and skip directories that have not
  changed since the last pass (adding a file changes its parent's mtime).

### 2.5 Reorganize depth: done

Reorganize mode prunes the walk by depth below each ID. Nothing below
`reorganize_depth` is walked at all, and neither are `NN.00` folders,
hidden folders or git repositories. On the test archive:

| scope | files the model would see |
| --- | --- |
| depth 0 (default) | about 2,000 (scan 2 s) |
| depth 1 | about 13,000 (scan 11 s) |
| everything | about 800,000 (scan 69 s; about 90 days of model time on the 35B) |

A single area or category can be reorganized on its own: give it as SOURCE
with the archive as TARGET.

## 3. Identify (runs ahead of the model; matters for media)

- **Persistent exiftool.** `exiftool -stay_open True -@ -` avoids a Perl
  start-up (~0.2 s) for every photo and video.
- **Fewer processes per photo.** A photo currently runs `identify`,
  `exiftool` and `magick`, plus `file` when libmagic is missing.
  Dimensions already come from exiftool, so `identify` can be dropped.
  Videos run `ffprobe` twice: once for metadata, once for the duration.
- **Hash lazily.** Every file up to 256 MB is fully read for SHA-256, only
  to detect duplicates of files sorto already filed. Hashing only when a
  file of the same size has been filed before saves reading most videos.
- **Duplicates of big files.** Files over `hash_max_mb` get a sampled hash,
  which `_handle_duplicate` ignores. Big video copies are never recognised
  as duplicates. A full hash for size-matched candidates would fix this.

## 4. Correctness and robustness

- **Git repositories: done.** A repository (working tree, linked worktree
  or bare) is never walked into, when sorting and when reorganizing; nothing
  is filed into one; and the same check runs right before every move. It is
  built in and no rule or setting changes it. Still open: moving a
  repository *as a whole* into the archive. Today it stays in the source.
- **Folders as units: done.** See 1.5.
- **Protected IDs.** In reorganize mode, sync folders (camera uploads, a
  folder another device syncs) should never be emptied. Today this is done
  with `--exclude` or a rule the model has to follow. A deterministic
  `protect = ["05.11", "05.12"]` setting would be safer.
- **Retrying when the model is down.** `_RetryLater` sends the file back
  through identify, so every retry re-hashes the file and re-renders
  previews and video frames. The packet could be kept and re-queued
  straight to the sort worker.
- **Inode reuse.** `upsert_discovered` treats a file with the same
  inode, size and mtime as a finished one as that file moved. A new file
  that reuses a freed inode with identical size and mtime would be skipped.
  This is rare, but checking the hash on that path would close it.
- **TUI polling on large databases.** Every 0.4 s the TUI runs `counts()`
  (`GROUP BY status`) and `recent()` (`ORDER BY updated_at` with no index),
  and `_wait_drained` polls `is_idle()` every 0.1 s. That is fine for
  thousands of rows. For an 800,000-row reorganize database, an index on
  `updated_at` and counters kept in memory would avoid full-table work
  several times a second.
- **Dead code.** The symlink branch in `iter_regular_files` resolves the
  link target and then skips it either way.
- **Maildir files in the ETA.** Emails without an extension are counted as
  "other" when estimating time left. The kind is only known after identify.

## 5. Done

- Reorganize mode (`sorto run DIR -t DIR`) with depth limits, a higher
  confidence bar for moving already-filed files, and no shuffling between
  subfolders. An area or a category given as SOURCE is reorganized in place.
- Free-form user rules (`~/.config/sorto/rules.md`), shown to the model
  after the outline. The rule the model followed is shown in the TUI. A rule
  can ask for an ID of its own; sorto creates it for the first such file.
  `sorto rules` and `sorto doctor` check the rules.
- Junk patterns (`junk.md`): matching files go straight to one ID.
- Email headers (From, To, Subject, Date) and body for `.eml` and Maildir
  files, recognised by content.
- Whole folders are judged once and moved together when they are coherent.
- The user's own photos and videos are filed into `YYYY/MM` by capture date.
  The date comes from the metadata or a camera-style file name, never from
  the model or the file's modification time.
- The place of a GPS position comes from a city list shipped with sorto.
- A time-left estimate from the measured pace per kind of file, with a
  finishing time.
- Switching models on the fly in the TUI (`m`), per-model answer times, and
  context size read from the Ollama tag.
- `sorto doctor` loads every configured model once, checks it answers, and
  reports load time, GPU share and whether switching needs a reload.
- A warm-up request at start and after a switch.
- New IDs: when no ID fits, sorto creates the next free ID in a fitting
  category before moving the file. The name is settled in one focused
  question, an answer that is not an ID at all is treated as a request for
  a new one, and when no category fits a new category is created in an
  existing area. If none of that works out the file stays and the log says
  why. The structure model decides all of it.
- A target with no IDs at all gets a proposed structure (`sorto init`),
  created only when the user agrees.
- One readable log per run (`runs/run-<date>_<time>.log`), and the TUI can
  browse back through the files of the run.
- `--retry-kept` asks again about files left for the user, `--clear-cache`
  forgets cached answers and folder decisions.
- Moves between btrfs subvolumes use a reflink clone (`FICLONE`) instead of
  copying every byte. A normal copy remains the fallback for other file
  systems.
- Git repositories are never touched.
- A rule can ask for named folders inside an ID, and the rule a file followed
  is shown in full.
- 3D model files are read: 3MF title, parts and preview picture, STL header.

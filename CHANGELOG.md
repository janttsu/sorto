# Changelog

## Unreleased

- `--model NAME` (also `--llm-model`) for `run`, `resume` and `fsck`. Without
  it the TUI asks at the start which local model to use, listing every model
  the server has with its size and marking the default and the one in memory.
  The `m` key that switched models during a run is gone.
- The model decides the way a Johnny.Decimal user does: first the category,
  then the ID in it (a `category` field comes before `jd_id`). The prompt
  now explains the standard zeros, broad IDs with one subfolder per trip,
  project or model, subfolder names with a pattern (date first for trips and
  events), and file names in the Johnny.Decimal style, with four short
  examples. New IDs are named after the kind of thing they hold; a new
  category needs a good reason.
- With Ollama the answer is constrained to a JSON schema: `jd_id` can only
  be an ID of the outline or "new", so a made-up ID cannot be written.
- The file packet is leaner: no hash, no second type field, and no hex dump
  or magic string when the type is known. Summary and reason are shorter.
- `sorto fsck`: the structure review is told the Johnny.Decimal principles
  and the next free numbers, and a proposal that names an ID that does not
  exist gets a warning.
- `sorto fsck TARGET` first has the big model review the tree (progress bar
  and ETA): a description for every ID without one, which name is right
  where a note and a folder disagree, duplicates, misplaced IDs, and, after
  thinking about the whole tree, proposals for a simpler structure or bigger
  changes to the numbering, shown as text. It then checks the structure and
  the JDex notes:
  it proposes missing entries, removes entries whose ID is gone (or gives them
  their new number), fixes names, and creates missing category notes. Each
  file's change is shown as a coloured diff in a TUI and written only when
  accepted (`--no-tui` asks as plain text); `--yes` writes them all. Previous versions are kept in `~/.sorto/fsck-backups/`.
  Generated notes, prose and folders are never changed; structure problems
  are reported. `sorto run` says when the notes and folders disagree.
- ID descriptions are also read from table rows, and a category's own note
  describes its IDs before a tree-wide index does.
- Before a new ID is made, sorto asks whether an ID of that category already
  holds this kind of file. If one does, the file goes into a folder named
  after the topic inside that ID instead (one more 3D model into the ID for
  models, not an ID per model). `new_subfolders = "off"` puts it into the ID
  itself. A rule that explicitly gives a topic an ID of its own still gets one.
- At the end of a run sorto says what it leaves in the source and why:
  duplicates and junk kept earlier (and the option that removes them), files
  left for you (`--retry-kept`), files with an error (`sorto resume`), and the
  git repositories and software packages it does not enter, with their file
  counts. Printed at the end of the run and written to the run log, so a run
  that handled no files explains itself.
- Unpacked software is kept whole like a git repository: a `squashfs-root`
  folder, a folder with an `AppRun` file and a `usr/` folder (an extracted
  AppImage) or a copied Unix root (`usr/lib` with `usr/bin` or `usr/share`)
  is never walked into or filed into. Before, its libraries and icons were
  filed one by one all over the archive. A Johnny.Decimal area, category or
  ID folder never counts as one, whatever stray files are in it.
- A file that is no longer in the source when its turn comes (moved or
  deleted by someone else) is counted as gone, not as an error. The run log
  gives one line for all of them instead of one per file. Older indexes are
  converted when opened.
- Ollama is spoken to through its own `/api/chat`: the model stays loaded for
  as long as sorto runs (`keep_alive = "run"`) and is let go `keep_alive_after`
  the run. `num_ctx` and `num_gpu` can be set. Other servers keep the
  OpenAI-compatible endpoint.
- A rule can ask for files to be kept in folders named after what they are;
  sorto creates such a folder inside the ID. `new_subfolders = "off"` turns
  it off.
- The `Rule:` line shows the whole rule as written, not the model's short
  quote of it.
- 3D model files: the title, part names and stored preview picture of a 3MF,
  and the header of an STL, are read and shown to the model.
- A folder the model suggests that does not exist is no longer dropped
  silently: the log says the file went to the ID itself.

## 0.1.0b1

First public beta. sorto works, but it is young: use `--dry-run` or `--confirm`
first, and expect settings and behaviour to change before 1.0.

What it does:

- Files loose files into an existing Johnny.Decimal tree with a model that
  runs on your own machine (Ollama). Nothing is sent anywhere else.
- Looks inside files: document text, email headers, photo and video metadata,
  the pictures and video frames themselves.
- Never overwrites or deletes. Uncertain files stay where they are, with the
  reason shown. Git repositories are never touched.
- Follows your own rules, written in plain language in `rules.md`, and file
  patterns in `junk.md`.
- Judges whole folders once and moves coherent ones together.
- Files your own photos and videos by capture date into `YYYY/MM`, and tells
  the model which city a GPS position is in.
- Creates a new ID, or a new category, with the next free number when nothing
  existing fits. These decisions are always made by the bigger model.
- Reorganizes an existing archive, or one area or category of it, in place.
- Proposes a Johnny.Decimal structure for a tree that has none (`sorto init`).
- Shows every file in a terminal UI with its analysis, destination and reason,
  lets you browse back through the run, and writes one readable log per run.

Known limits:

- The model's judgement decides where a file goes and it is not always right;
  the smaller model more often so.
- A git repository is left where it is; sorto does not move it as a whole.
- Tested on Linux only.

# Changelog

## Unreleased

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

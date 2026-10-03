# sorto in detail

The [README](../README.md) has the short version. This page covers everything else.

## Privacy: local model only

- The LLM URL must resolve to a **loopback address** (`127.0.0.1`, `::1`, `localhost`). sorto refuses any other host, including LAN machines and cloud APIs, and ignores `HTTP(S)_PROXY`.
- Only a compact packet leaves the file system, and only to that local server. The packet holds the name, type, size, dates and tool metadata. It also holds a short preview: the first 2 pages of text for PDFs, the main text for DOCX/ODT, and a few KB of plain text.
- sorto keeps its own index in `~/.sorto/`, never in the source or the target, so nothing extra ends up in a synced archive. See [What sorto remembers](#what-sorto-remembers).

## Starting without a structure

If the target has no Johnny.Decimal areas, categories or IDs, `sorto run` (or `sorto init TARGET [SOURCE]`) offers to create them:

1. **Survey.** sorto counts the files in every folder, three levels deep, with types, years and sample names. Every top-level folder is listed with its totals first, so the big parts of the tree are never cut from the overview. Git repositories are counted but not opened.
2. **Proposal.** The local model designs a small structure for that content: a few areas, categories inside them, and IDs with one-line descriptions. Dated folders go into one ID (the years become subfolders), and things your rules set aside share one area. Your rules and the language of your folder names are taken into account.
3. **Numbering.** sorto renumbers everything itself: areas `X0-X9`, categories inside their area, IDs `.11, .12, …` without gaps, names cleaned to safe folder names.
4. **Your decision.** The tree is printed and saved to `~/.sorto/<pair>/structure-proposal.md`. Nothing is created until you answer `y`, or pass `--accept-structure`. To change names or IDs first, edit that file and run `sorto init TARGET --from FILE`. Your numbers are kept, but checked: every category must be inside its area, every ID inside its category, and no number may be used twice.
5. **Creation.** The folders are made (nothing that exists is touched) with a `JDex.md` holding the descriptions, which sorto then reads like any JDex note. After that the run continues and sorts into the new structure. When source and target are the same folder, loose folders are judged as a whole first (see below).

`--dry-run` only shows and saves the proposal.

## Whole folders first

Files that belong together (an album, one trip or event, a project, a correspondence, a monthly dump of downloads) are not scattered file by file. When sorting reaches a folder, sorto first looks at it as a whole, from the outside in:

1. **Size check.** A folder with fewer than `folder_min_files` (5) files is sorted file by file. One with more than `folder_max_files` (2000) is too big to judge at once, so its subfolders are judged one by one.
2. **One question about the folder.** The model gets the file list, file types, modification dates, and the EXIF capture dates, cameras and GPS spread of a sample. It also gets a few text excerpts and up to three pictures. It answers whether the files belong together, what the folder is and which ID it belongs in. This question runs at temperature 0, so the same folder always gets the same answer. The same system prompt is used, so the cached outline and your rules apply.
3. **A coherent folder with confidence of at least `folder_min_confidence` (0.8)** moves into that ID as one folder, keeping its name and inner structure (`14.11 Paris 2025/Paris trip 2025/…`). An existing folder of the same name is never merged into; the new one becomes `name-2`. If no ID fits, the new-ID rules below apply. `--confirm` asks once for the whole folder. The one exception: when the folder holds your own photos and videos, those go by date into `YYYY/MM` inside that ID (see below), and only the other files keep the folder.
4. **Every file is still checked on its own**, without a model call. sorto compares its type and EXIF data to the folder's profile, which is built from quartiles and medians so the outliers in the sample do not stretch it. A file is sorted on its own, with the folder's description as context, if:
   - its type is a small minority in the folder (for example a PDF among photos; photos and videos count as one type);
   - its capture date lies outside the folder's dates (at least 60 days of margin);
   - its GPS position is far from the rest (at least 50 km away);
   - it was taken with a camera or phone when the folder's other files record none (your own video in a folder of downloads);
   - it records no camera at all when the folder's other photos and videos do (a received picture among your own shots).
5. **A file that stands out is checked, not thrown out.** The model is told what the folder is and why the file stands out. If it still files the file into the folder's own ID, the file keeps its place inside the folder.
6. **Some folders only work in one piece:** software, source code, a website, a game, application data, a system backup. The model marks such a folder (`intact`), and then nothing is taken out of it, not even a picture with a camera in its EXIF. A folder with program code in it (at least 3 code files and 10% of its files) is kept in one piece whatever the model says.
7. **A mixed folder** is sorted file by file, and each file gets the folder's description as context.

Decisions are remembered in the index, so a later run treats new files in a moved folder the same way. A dry run shows the decisions but does not store them. Files that move with their folder skip the expensive steps (previews and document text), because only the outlier check needs them, and that uses EXIF.

`--no-folders` or `folder_mode = false` sorts every file on its own, as before. When reorganizing, only folders outside the Johnny.Decimal structure are judged as a whole; what is already filed is re-checked file by file. A folder without subfolders is judged as a whole even when it is bigger than `folder_max_files` (up to 250,000 files), because it cannot be split any further. Git working trees are never walked into or moved piece by piece.

## Reorganize a tree in place

Give the same directory as SOURCE and TARGET and sorto tidies that tree instead of emptying an inbox:

```
sorto run ~/Archive -t ~/Archive --dry-run     # see what would move
sorto run ~/Archive -t ~/Archive --confirm     # ask before every move
sorto run "~/Archive/10-19 Life/14 Travel" -t ~/Archive --dry-run   # only that category (or an area)
```

**One area or category.** When SOURCE is an area or a category of TARGET, only that part is reorganized, by the same rules as below: its IDs stay valid choices, a file that is already in the right ID stays, and nothing outside the part is looked at. Earlier versions treated such a source as an inbox. That hid the category's own IDs from the model, so every file had to leave: a boarding pass in `14.11 Travel` could only be answered with `14.11`, which was "not an ID in the target". Such a run keeps its own index under `~/.sorto/` (`14_Travel_in_place-…`).

- **Loose files** (in the root, or directly in an area or category folder) and files in **`NN.01` inboxes** are filed into the best ID, just like inbox files.
- **Files already inside an ID** are re-checked. The model is told where each file is now (`Was in:` in the TUI) and asked to keep it there unless it clearly belongs elsewhere. It stays unless the model picks a *different* ID with confidence of at least `reorganize_min_confidence` (default 0.75, higher than the normal 0.5). Within its own ID a file only moves from the ID folder into an existing subfolder, never from one subfolder to another.
- **Only the top of each ID is re-checked.** By default that means files directly in the ID folder. `--depth 1` also checks one subfolder down, such as `13.13 Invoices/2024/`. Deeper folders are usually projects, backups, albums or copied trees that belong together, so they are never picked apart file by file. On an archive of about 800,000 files, depth 0 means about 2,000 files and depth 1 about 13,000.
- **Never touched:** `NN.00` index folders, JDex notes, hidden folders, git working trees, and folders named like an ID that are not part of the outline (for example a duplicate ID).
- Reorganizing is one pass (`--once` is the default here). Files that are already in place show as `ALREADY IN THE RIGHT PLACE`.

 Anything the model would move with too little confidence shows as `LEFT WHERE IT IS`, with the suggestion in the reason.

To protect a sync folder, exclude it (`--exclude "05.11*/**"`) or write a rule such as "keep everything in 05.12 where it is".

## Your own rules

`~/.config/sorto/rules.md` (or `$XDG_CONFIG_HOME/sorto/rules.md`, or `--rules FILE`) is a free-form text file. Write sorting rules there in plain language, in any language, and the local model follows them for every file:

```markdown
- Emails from billing@example.com go to 13.13.
- Anything that mentions my employer Example Ltd goes to 21.11.
- Screenshots always go to 51.12, never to 51.11.
- Photos taken between 2025-06-01 and 2025-06-14 belong to 14.12 (summer trip).
- Scanned receipts go to 13.13, into the year folder of the receipt date.
- Everything about Example Club goes under an ID of its own.
```

- **Rules can be about anything the model can see:** names, file types, the text inside documents, email senders and subjects, photo dates, GPS positions, camera models and so on.
- **Rules come after the outline in the system prompt and take priority** over the model's general judgement. They cannot widen what sorto may do: the model still never chooses a number or a path, and uncertain files still stay where they are.
- **A rule can ask for an ID of its own.** "Everything about X goes under its own ID" makes the model answer with a new ID for the first such file, if the target has none for X yet. sorto then names, places and numbers it as any other new ID (see [It respects your existing organization](#it-respects-your-existing-organization)), and later files go to the same ID. The file is not parked in another ID meanwhile. This reaches only files the model looks at one by one: files that move with their folder as a whole are not checked against the rules individually.
- **You can see which rule was used.** When the model follows a rule, it quotes it briefly. sorto finds the rule that quote comes from and shows it on the `Rule:` line in full, as you wrote it, in the TUI and in the run log.
- **A rule can ask for named folders.** "Knitting patterns go to 32.11, each in a folder named after the garment" makes the model name a folder for each file, and sorto creates it inside the ID: `32.11 Patterns/Wool socks/`. This is the only way a new folder with a name is made, and only when the rule the model quotes really is one of yours. sorto decides what is a usable name (one folder level; not a date folder, not something that looks like an ID), uses a folder that already goes by that name whatever its spelling (`wool-socks`), and logs each folder it creates. When reorganizing, a file in the root of its ID moves into the folder its rule asks for. Without such a rule a folder the model suggests is not created; the file goes to the ID itself and the log says so. `new_subfolders = "off"` turns this off.
- **Commented lines are ignored.** Text inside `<!-- -->` is not sent to the model.
- **Edits apply while sorto runs.** The file is re-read every minute, and changed rules are used from the next file on. The log shows `re-read … rules.md: N rule line(s)`, and the title bar shows the new count. Rules come after the outline in the prompt, so the model keeps its cached outline and only reads the rules again.
- **`sorto rules`** creates a commented template with examples and prints the active rules. `sorto rules /path/to/jd-root` also warns about IDs that do not exist in that tree. `sorto doctor` checks the same.

Rules are read by intent, like instructions to a person. A model may apply a rule to a closely related file as well. In the demo, "Emails from billing@northwind.example are bills" was also cited for a PDF bill from the same company, by both the 9B and the 35B. If a rule must be strict, say so in the rule itself ("only emails, not attachments or PDFs").

### Junk patterns: `junk.md`

`~/.config/sorto/junk.md` (or `--junk-rules FILE`) is for files you never need, whatever they are: build leftovers, `*.dll`, `*.pdb`, thumbnail caches. Every matching file goes straight into one folder of your target without a model call; it is moved there, never deleted, so you can still look at it.

- `into: NN.NN` names the folder (an existing ID). Then one pattern per line, matched case-insensitively against the file name with shell wildcards (`*.dll`, `Thumbs.db`, `*.tmp`). A pattern with a `/` is matched against the whole path inside the source (`build/**/*.o`).
- `pattern -> NN.NN` sends that pattern to an ID of its own.
- Text inside `<!-- -->` is ignored. `sorto rules` creates a commented template and lists the active patterns; `sorto rules TARGET` and `sorto doctor` check that the IDs exist. A pattern whose ID does not exist keeps its files where they are, with the reason shown.
- Junk patterns win over everything else: a matching file inside a folder that moves as a whole still goes to the junk folder, and one already filed elsewhere is moved there when reorganizing. Only the built-in junk list (`.DS_Store` and the like, which stays in the source unless `--delete-junk`) comes after it.
- The file is re-read every minute, like `rules.md`. Files it matches skip previews and document text, since the model is not asked about them.

Emails are read as emails. `.eml` files and Maildir files without an extension are recognised by their headers, and the model gets the decoded From, To, Subject and Date plus the body text, so sender rules work.

## Photos and videos: EXIF, GPS and what is in the picture

This is always on. For every photo and video sorto collects the following:

- **Named EXIF / XMP / QuickTime fields** (`exiftool -n`):
  - capture time; video times are converted from UTC to local time
  - camera or phone
  - GPS latitude, longitude and altitude in decimal degrees
  - the place at that position, worked out by sorto: `gps_place`, for example `Oslo, Norway (3 km away)`
  - captions, keywords and titles
- **A preview the model can look at.** Each photo is sent as a downscaled image, 768 px on the longest side (ImageMagick, EXIF-rotated). Each video gets **3 frames** taken at 10%, 50% and 90% of its length (ffmpeg).

`qwen3.6:35b-a3b` supports images, so the model sees the photo or the frames together with the capture date and GPS position. It uses them to recognise the trip, event or theme, for example a photo taken in Rome during May 2024 means `14.12 Rome 2024`. It also describes in English what it actually saw. The TUI shows a `Media:` line such as `taken 2024-05-14 15:02, GPS 41.9028,12.4964 = Rome, Italy (2 km away), Google Pixel 8, model viewed the image`.

How this works in practice:

- **Nothing leaves the machine.** Everything is created in memory and sent only to the local model. No thumbnails are written anywhere.
- **sorto names the place, not the model.** A model handed bare coordinates guesses, and guesses towards whatever place is on its mind: with an ID for a trip to one city in the outline, it calls a position 2,000 km away "the area" of that city, and photos from somewhere else end up under that trip. So sorto looks the position up itself, in a list of 31,761 cities of at least 15,000 people that ships with it, and gives the model the answer: the biggest city within about 20 km, otherwise the nearest one, with the distance. The model is told to name no other place from the GPS position, and that a photo belongs to a trip ID only when the place and the date fit. No online service is used. The city data is from [GeoNames](https://www.geonames.org/), licensed CC BY 4.0; `scripts/build_cities.py` rebuilds it. A place is still never put into a filename unless a caption or a sign in the picture says it.
- **Speed cost.** A photo adds about 450 prompt tokens, and a video adds about 3 × 250. On the test machine that is roughly 5 seconds more per media file.
- **Opting out.** `--no-vision`, or `vision = false` in the config, stops sending pixels. The EXIF fields are still used.
- **Tuning.** `preview_px` and `video_frames` set the preview size and the number of frames.
- **Phone names stay.** Phone camera names such as `20240102_030405.jpg` carry the capture time, so they are never renamed.

### 3D model files

Files for 3D printing are often named badly (`plate_1.3mf`, `final2.stl`), so the name alone says little. sorto reads what the file says about itself, in memory, without unpacking anything:

- **3MF:** the title, description and part names, and the preview picture that slicers store in the file. The picture is sent to the model like a photo, so it sees the object.
- **STL:** the header text or the `solid` name, when there is one. An STL holds nothing else.

`--no-vision` leaves the picture out; the text is still used.

### Your own photos and videos go into `YYYY/MM`

Photos and videos you shot yourself are always filed by capture date: `51.11 Photos/2024/06/20230815_102030.jpg`. This holds for every ID the model picks, for new IDs, for files that arrive inside a folder, and when reorganizing.

- **The model says whether it is yours** (`own_media` in its answer). It sees the picture, the path, your rules and a `camera_signs` line with the evidence: the camera or phone, a GPS position, a file name as cameras write it. Phone videos count too, although they record no `Make`/`Model` (Android and Samsung write their own fields). Screenshots, memes, received or downloaded pictures and clips, AI images, films, scans and pictures that belong to software are not yours, even when a camera is recorded. If the model leaves the answer out, camera evidence decides.
- **sorto works out the date, never the model.** It takes the capture date in the metadata, else the date in a camera-style file name (`VID_20210304_050607.mp4`). Impossible dates (before 1990, in the future, `0000`) are skipped. A photo or video with no capture date gets no date folder and goes to the ID itself: the file's modification time is not used, because for a copied, exported or downloaded picture it is the day of the copy. For the same reason a year folder the model suggests for a photo or video is only accepted when the file itself carries that year. A subfolder the model suggests is ignored for these files.
- **Folders.** The folder question also asks whether the folder's photos and videos are your own shots. If so, each one goes to `YYYY/MM` inside the folder's ID without a model call of its own, and the rest of the folder (notes, documents) moves as a folder. A folder that is not yours, such as a source tree with example pictures, stays in one piece.
- **Reorganizing.** A photo of yours in the root of its ID, or in the wrong folder within reach of `--depth`, moves to its `YYYY/MM`. One already there stays.
- **Nothing is overwritten:** two files with the same name in the same month become `name.jpg` and `name-2.jpg`.
- **IDs that are always by date.** The model's answer is a judgement and can be wrong. For the IDs that hold your own photos and videos you can make it certain: list them in `date_folder_ids`, and every photo and video that lands there goes into `YYYY/MM`, whatever the model said. Write the ID with its name (`"51.11 Photos"`) so the setting cannot hit another archive by accident; a bare `"51.11"` works too.
- **Settings.** `date_folders = "own"` is the default. `"all"` files every photo and video by date, `"off"` none.

```toml
[run]
date_folder_ids = ["51.11 Photos", "54.11 Own videos"]
```

## It respects your existing organization

sorto reads the structure of the target at run time. Nothing about any particular tree is hardcoded.

- **Areas** `NN-NN Name`, **categories** `NN Name` and **IDs** `NN.NN Name` are all discovered from the target, including IDs that sit directly in the root, such as `05.11 Shared folder`.
- **JDex notes** are read from every `*JDex*.md` in a `NN.00` folder or in the root. A line like `` - `13.13` Invoices — all personal bills `` becomes that ID's description in the model's prompt.
- **Existing subfolders** of each ID are listed for the model, except year and year-month folders: for those the outline only says that the ID has them (`[has year folders (YYYY)]`), not which years exist. Date folders are how files are stored, not what an ID is about, and a model takes "already has a 2023 folder" as proof that photos from 2023 belong to a trip made in 2024. sorto only files into subfolders that already exist, unless one of your rules asks for named folders (see [Your own rules](#your-own-rules)). It creates a new year or year-month folder (`2025`, `2025-03`) only when that ID already uses that pattern. Your own photos and videos are the exception: they always get `YYYY/MM`.
- The model picks an **ID** and sorto builds the path itself. The model can never invent an area, a number or a path. `NN.00` index folders are never used. `NN.01` inboxes are used when the category is clear but no specific ID fits a one-off file.
- **New IDs, numbered by sorto.** When no existing ID fits and the file clearly starts a topic of its own, the model may ask for a new ID. It names only an existing **category** and a **name**. sorto then:
  1. asks the model once more, with only the categories and their IDs in view, which category the new topic belongs in (at temperature 0). This focused answer decides. Low confidence keeps the file where it is; `none` leads to a new category, see below. In testing, both local models put a workout log under "Code" when choosing inside the full classification, but "Health" when asked on its own;
  2. takes the next free number after the category's highest ID (at least `.11`; `.00`–`.10` stay reserved), checking the folders on disk as well as its index;
  3. reuses an ID in that category that already has the same name, instead of making a second one;
  4. creates the folder (`13.14 Newsletters`), re-checking right before that the number is still free, logs it in `progress.jsonl`, and re-reads the tree;
  5. only then moves the file in. Later files see the new ID like any other.

  **The name is settled in one focused question.** Before those steps, sorto asks the structure model (see below) what the new ID should be called. The question shows the categories with their IDs, your rules, the file's summary and the model's answer, including a name it may have suggested. This is done for every new ID. Names made up file by file drift: in testing, one rule ("everything about X goes under its own ID") produced four differently named IDs in six files. Asked this way, the model names the ID after the rule's subject, and if exactly one ID in the target already has that name, the file goes there. Answering with the name of an existing ID joins it. A name without letters (`07`, `2234489_5825`) is not accepted, and neither is a name that only repeats its category (`51.20 Pictures` inside `51 Pictures`); the file then stays where it is.

  **When the answer is not an ID at all.** Local models sometimes answer with a number that does not exist (`51.02`), an area (`90-99 Archive`) or a path (`90-99 Archive/93 Mail`), without saying what the new ID should be called. This is treated as a request for a new ID: the same question gives it a name, and the steps above place and number it. If the model gives no usable name, the file stays where it is.

  **When no category fits: a new category.** If the category check answers `none`, sorto asks one last question: which existing area does a new category for this topic belong in, and what is the category called? The question shows the areas with their categories and your rules. sorto then numbers it: the category gets the number after the area's highest (a gap is not filled, it may be a number you retired; `N0` stays the area's own), and the new ID is its first regular one, `.11`. An ID lying in the root counts for its category (`05.11 Shared folder` means 01 is taken). A category of that name that already exists in the area is used instead. sorto creates `90-99 Archive/94 Pets`, then `94.11 …` inside it, logs both, and moves the file. It never creates an area, and it does not add `NN.00` or `NN.01` folders or JDex lines to the new category. `allow_new_categories = false` or `--no-new-categories` turns this off.

  **When that does not work either, the file stays.** The model answers `none` (no area fits, or it cannot name a sensible category), its confidence is below `new_id_min_confidence`, or the area has no free number: nothing is created, the file stays where it is, and the reason is written to the run log and `progress.jsonl`.

  **The big model decides.** Naming a new ID, choosing its category and inventing a category are always done by `structure_model` (`[llm]`, default `qwen3.6:35b-a3b`), also when you read the files with the small model: a new ID or category stays in the archive for good, and in testing the small model placed them poorly. A name the small model suggested is passed on as a suggestion only. On a GPU that holds one model at a time, Ollama swaps the models for these questions, so each new ID costs two model loads (about a minute); files that go to existing IDs are not affected. `structure_model = ""` lets the reading model decide. If the structure model is not on the server, the log says so and the reading model decides.

  A new ID needs confidence of at least `new_id_min_confidence` (default 0.8). `--confirm` asks before creating it, `--dry-run` only shows it (`New ID:` in the TUI), and `--no-new-ids` or `allow_new_ids = false` turns it off. A made-up number from the model (say `40.36`) is never used; the file either gets the next free number in category 40 or stays where it is. sorto does not edit your JDex notes, so add new IDs there yourself (or to the script that generates them).
- **A target without any IDs is refused.** `sorto run` stops at once with an error instead of asking the model to file into nothing.
- **A source inside the target** depends on what it is. An ID folder (for example an inbox, `05.12 Incoming`) or a folder without IDs is sorted out: it is left out of the choices and its files go elsewhere. An area or a category (`10-19 Life`, `14 Travel`) is a part of the archive, so that part is reorganized in place; see [Reorganize a tree in place](#reorganize-a-tree-in-place). If the target is inside the source, it is left out of the scan.

Run `sorto index /path/to/jd-root` to see the exact outline the model gets.

## Install

The simplest way is `pipx install git+https://github.com/janttsu/sorto.git`. To work on the code, use a virtual environment (Arch and other PEP 668 distros refuse a system-wide `pip install`):

```bash
cd sorto
python3 -m venv .venv
source .venv/bin/activate          # fish: source .venv/bin/activate.fish
pip install -e ".[dev]"
sorto --help
```

`make install` does the same. Optional helpers: `pdftotext` / `pdfinfo` (poppler), `exiftool`, `ffprobe`, `identify` (ImageMagick) and `file`.

## The model: Qwen 3.6 35B on Ollama

The default is `qwen3.6:35b-a3b` on a stock Ollama at `http://127.0.0.1:11434/v1`. Use `--llm-url` / `[llm] url` for another local server, or let sorto take it from your Grok Build profile (below).

### Two models, switchable on the fly

`[llm] models = [...]` lists the models the TUI can switch between. Press `m`: the file being analyzed finishes with the current model, the next one loads in the background, and every later file uses it. The title bar shows the active model. The progress line compares the average answer time per model (`qwen3.6:35b-a3b 10.2s×14 | qwen3.5:9b-16k 3.6s×9`), and each analysis shows which model wrote it and how long it took. That way you can judge speed and accuracy on your own files.

Whichever model reads the files, new IDs and categories are named and placed by `[llm] structure_model` (default `qwen3.6:35b-a3b`); see [It respects your existing organization](#it-respects-your-existing-organization).

Measured on a desktop with a 6 GB GPU and 31 GB RAM with a sorto prompt of 6,600 tokens and an invoice PDF:

| model | how it runs | per file (outline cached) | first file (outline not cached) |
| --- | --- | --- | --- |
| `qwen3.6:35b-a3b` | MoE, experts on the CPU, 30 tok/s | ~10 s | ~60 s (16 s load + 40 s outline) |
| `qwen3.5:9b` as pulled | 64k context, 3.7 of 6.6 GB on the GPU, 8 tok/s | ~20 s | ~60 s |
| `qwen3.5:9b-16k` | 16k context, `num_gpu 99`: all on the GPU, 47 tok/s | **3–4 s** | ~25 s (15 s load) |

On six generic test files (a bill email, a bank statement, a doctor's note, a shell script, meeting notes and a photo with Paris GPS), both models chose the same ID for every file and both followed a sender rule. The 35B additionally used an existing year folder. Try both on your own files and compare.

The small model is only fast when it fits the GPU completely, so it needs its own Ollama tag with a smaller context:

```bash
printf 'FROM qwen3.5:9b\nPARAMETER num_ctx 16384\nPARAMETER num_gpu 99\n' > Modelfile.9b-16k
ollama create qwen3.5:9b-16k -f Modelfile.9b-16k             # add OLLAMA_HOST=… for a non-default server
```

sorto reads `num_ctx` from the tag and sizes its prompt to fit. 16k is enough: the outline, one file packet, three video frames and the answer come to about 10k tokens.

`sorto doctor` loads every configured model once, checks that it answers, and reports its load time and how much of it sits on the GPU. It finishes by loading the default model, so a run that starts now finds it ready. On a 6 GB GPU the two models cannot stay loaded at the same time, so doctor reports how long a switch takes to reload. `--no-warm` skips this.

### Keeping the model hot

- **The model stays loaded for as long as sorto runs.** When the server is Ollama, sorto talks to its own `/api/chat` instead of the OpenAI-compatible endpoint, because only that one honours `keep_alive` per request (the OpenAI endpoint ignores it; tested). Every request asks the model to stay loaded, so a pause, a long `--confirm` wait or a quiet hour in follow mode no longer unloads it. No server-wide setting is needed.
- **When the run ends the model is let go.** sorto sets its unload timer to `keep_alive_after` (5 minutes, Ollama's own default). It has to be a real value: Ollama keeps the last `keep_alive` it was given for a loaded model, and a request without one does not bring the server's default back (tested). A model that was already loaded for good before sorto used it is left that way, and stopping sorto never loads a model.
- **Settings** (`[llm]`): `keep_alive = "run"` is the default. `"45m"` or a number of seconds asks for that on every request and leaves the timer alone at the end; `"-1"` keeps the model for good; `""` sends nothing, so the server's `OLLAMA_KEEP_ALIVE` applies. `api = "openai"` forces the OpenAI-compatible endpoint, `"ollama"` the native one; the default `"auto"` asks the server once.
- **Other servers** (llama.cpp, LM Studio) get the OpenAI-compatible `/v1/chat/completions` as before.
- **The prompt cache matters more than the loaded model.** The Johnny.Decimal outline is the same for every file, and once it is cached a file costs its own packet plus the answer (about 10 s on the 35B). If anything else uses the same Ollama slot in between, such as Grok Build or another model, the next file has to re-read the whole outline: about 40 s on the 35B (up to 3 minutes while the machine is busy), about 6 s on the 9B.
- **sorto warms up at start.** A one-token request with the full system prompt loads the model and fills the cache while sorto is still scanning. It does the same after a model switch.
- **Your rules come after the outline.** Editing `rules.md` therefore only re-reads the rules, not the whole outline.

**sorto reuses your Grok Build settings.** If `~/.grok/config.toml` (or `$GROK_HOME/config.toml`) has a `[model."…"]` profile for the same model, sorto takes these values from it:

| Grok Build setting | sorto setting |
| --- | --- |
| `base_url` | `url` |
| `context_window` | `context_window` |
| `inference_idle_timeout_secs` | `timeout_sec` |
| `max_retries` | `max_retries` |
| `top_p` | `top_p` |

This way both tools share one loaded model with one KV-cache size. By default sorto sends no `num_ctx`, so it never forces Ollama to reload the model under Grok. On Ollama you can ask for one with `[llm] num_ctx = 16384` (and `num_gpu` for the number of layers on the GPU); this replaces a custom model tag when sorto is the only user of the model, and it applies to every model sorto uses, so keep tags when two models need different values. Grok's chat temperature is *not* reused, because sorto uses 0.6 for steadier answers.

Why these defaults:

- **`reasoning_effort = "none"`.** Qwen 3.6 thinks by default. With thinking on, it spends the whole token budget on hidden reasoning and returns an empty answer. With `none`, a JSON answer takes a few seconds.
- **The prompt stays well inside the context window.** The Johnny.Decimal outline goes in the system prompt, so Ollama's prompt cache reuses it for every file. A 114-ID tree with JDex notes comes to about 6k tokens. The outline is capped at about 80% of `context_window`.
- **One request at a time.** This matches `OLLAMA_NUM_PARALLEL=1`.

Check everything with:

```bash
sorto doctor ~/Downloads --target /path/to/jd-root
```

## Usage

```bash
sorto run SOURCE --target TARGET [options]     # watch SOURCE until you quit
sorto run SOURCE -t TARGET --once              # sort what is there now, then exit
sorto run SOURCE -t TARGET --dry-run --once    # show analysis + planned paths, move nothing
sorto run SOURCE -t TARGET --confirm           # ask before every move
sorto run DIR -t DIR --dry-run                 # reorganize DIR in place (preview)
sorto rules [TARGET]                           # show / create ~/.config/sorto/rules.md
sorto resume SOURCE -t TARGET                  # like run, and retry files that errored
sorto run SOURCE -t TARGET --retry-kept        # also ask again about files left for you to decide
sorto run SOURCE -t TARGET --clear-cache       # first forget cached model answers and folder decisions
sorto status SOURCE -t TARGET
sorto index TARGET                             # print the JD outline sorto sees
sorto doctor [SOURCE -t TARGET]
sorto config [SOURCE -t TARGET]                # merged settings; creates ~/.config/sorto/config.toml
```

| option | meaning |
| --- | --- |
| `SOURCE` | directory to sort (required, positional) |
| `-t, --target DIR` | Johnny.Decimal root to file into (required) |
| `--dry-run` / `--suggest-only` | analyze and plan only |
| `--confirm` | TUI asks before each move: `Enter`/`y` moves, `n` keeps the file in the source |
| `--min-confidence X` | below this (default 0.5) the file stays in the source |
| `--yes` | also move files the model flagged `needs_user` |
| `--no-vision` | do not show photos or video frames to the model (EXIF is still used) |
| `--no-rename` | never rename (by default only meaningless names like `IMG_1234` or `document(3)` are renamed) |
| `--no-new-ids` | never create a new ID; files that fit no existing ID stay where they are |
| `--no-new-categories` | never create a new category; a new ID is only made in a category that exists |
| `--retry-kept` | ask again about files an earlier run left for you to decide (`needs_user`) |
| `--clear-cache` | before the run, forget cached model answers and folder decisions; what was moved or kept is not forgotten |
| `--depth N` | reorganize mode: also re-check files N folders below each ID (default 0) |
| `--rules FILE` | use this rules file instead of `~/.config/sorto/rules.md` |
| `--delete-duplicates` | delete source files byte-identical to one sorto already filed; never inside a git repo |
| `--delete-junk` | delete `.DS_Store`, `Thumbs.db`, temp files, caches and similar; never inside a git repo |
| `--once` / `--follow` | exit when done / keep watching (default) |
| `--include GLOB` / `--exclude GLOB` | repeatable |
| `--max-file-mb N` | metadata only above this size (default 64) |
| `--llm-url URL` / `--llm-model NAME` | override the local endpoint or model |
| `--no-tui` | print each file's analysis and path as plain lines |

## TUI

Each file goes through the **NOW** panel: what it is, the English analysis, the chosen ID, the full destination path and why. **LAST FILED** keeps the previous file on screen, and **HISTORY** lists what this run has done, newest first (the last 500 files).

**Browsing the history.** `↓` (or `j`) shows the next older file in the lower panel, with its full analysis, destination and reason; `↑` (or `k`) goes back towards the newest. `PgDn`/`PgUp` step ten files, `End` jumps to the oldest. The shown file is marked `▶` in the HISTORY list, which scrolls with it, and the panel title says where you are (`HISTORY 14 of 230`). Files that finish meanwhile do not move the selection. `Esc` or `Home` returns to following the latest file. Earlier runs are in the run logs (see [What sorto remembers](#what-sorto-remembers)).

Keys: `q` quit (finishes the current file), `p` pause, `d` dry-run toggle (only when idle), `m` switch model, `o` progress log, `↑`/`↓` history, `?` help, and with `--confirm` `Enter`/`y` to move or `n` to keep.

**Time left** is estimated from the pace so far. sorto measures how long each file really took (analysis and move, not the time you spend answering `--confirm`), separately for photos, videos, documents and other files. It multiplies that by what is still waiting of each kind, for example `time left: ~3h 12m left for 812 files at 14.2 s/file (pace of the last 37 files), done ≈ 18:42`. The first answer usually includes loading the model and is left out of the pace. While the scan is still finding files, the line says so, because the total can still grow.

## Safety guarantees

1. **Never overwrite.** Files are placed with `renameat2(RENAME_NOREPLACE)`, which is atomic within one file system. Between btrfs subvolumes (where `rename` is refused) sorto makes a **reflink clone**: the new file shares the old one's data, so nothing is copied and no extra space is used. Across different file systems it makes a normal copy. Both clone and copy create the new file with `O_EXCL`, fsync it, and only then remove the original. If `renameat2` is missing, `link`+`unlink` is used. A name collision becomes `name-2.ext`.
2. **Never delete** unless you pass `--delete-duplicates` or `--delete-junk`, and never inside a git working tree.
3. **Git repositories stay whole and where they are.** This is built into sorto and cannot be switched off or overridden by a rule, a junk pattern or the model. A folder with a `.git` directory or file, or a bare repository (`HEAD`, `objects/`, `refs/`), is never walked into: nothing is moved out of it and nothing is moved around inside it, when sorting and when reorganizing. Nothing is filed into one either: a repository inside an ID is not listed as a subfolder, and if an ID folder is itself a repository, files chosen for it stay where they are. The same check runs once more right before every move, whatever planned it. Only repositories below SOURCE and TARGET count; an archive whose root is itself kept in git can still be sorted. sorto does not move a repository as a whole: it stays in the source for you to place.
   **Unpacked software is kept whole the same way:** a folder named `squashfs-root` or holding an `AppRun` file next to a `usr/` folder (an extracted AppImage), or a copied Unix root (`usr/lib` with `usr/bin` or `usr/share`). An area, category or ID folder of the archive never counts as one, so a stray `AppRun` filed into an ID does not close the ID. Its libraries, icons and configs only work together, so it is never walked into or filed into, and it stays in the source.
4. **Never modify contents.** sorto only moves files, and renames meaningless names. The original extension is always kept.
5. **Never leave the trees.** Sources must resolve under the source and destinations under the target, symlinked parents included.
6. **Structure grows only by sorto's own numbering.** Areas are never created. A new ID gets the next free number of its category, and a new category, made only when no existing one fits, gets the next free number of its area; both are created before the file moves (see above). The model never chooses a number or a path. Low confidence, `needs_user` answers and new IDs that cannot be placed leave the file in the source with the reason shown.
7. **Never extract archives.** DOCX and ODT text is read in memory. Nothing is unpacked to disk.
8. **Crash-safe.** A move is written to `progress.jsonl` (fsync'd) before the row is marked done. A clone or copy is written to a hidden `.name.sorto-partial-…` file and gets its real name only when it is complete, so a crash never leaves a half-written file under a real name. On the next start an interrupted move is finished (if the copy is byte-identical to the original), reset, or flagged when the two differ, in which case nothing is deleted. Leftover temp files are removed.

## What sorto remembers

sorto remembers every file it has handled, so running it again on the same SOURCE and TARGET continues where it stopped instead of asking the model again. For each pair it keeps a directory under `~/.sorto/` (`$SORTO_HOME` overrides it):

```
~/.sorto/Downloads-3f2a9c1b7e44/
  index.sqlite     files sorto has seen and what it decided, plus cached model answers
  progress.jsonl   append-only log of every move, keep and delete (fsync'd)
  runs/            one readable log per run, see below
  sorto.log        diagnostics
  config.toml      optional settings for this pair only
```

- **Files that were moved** are not looked at again.
- **Files that were kept** (low confidence, `needs_user`, left in place) are not asked about again unless they change (size or mtime). `sorto resume` also retries files that failed, and `--retry-kept` asks again about the files a run left for you to decide (`needs_user`), which is worth it after you changed the rules or the target, or updated sorto.
- **Where the index is:** every run prints the path, and `sorto status SOURCE -t TARGET` shows it too.
- **Fresh answers without starting over:** `--clear-cache` empties the two caches in the index before the run: the model's cached answers, and the remembered decisions about whole folders (moves as one, sorted file by file). Folder decisions do not expire on their own, so this is what makes sorto judge folders again after you changed the rules or updated sorto. A folder that moves as a whole and is already partly moved keeps its decision, so that the rest of it follows to the same place. The record of the files is untouched: moved files are not looked at again, and kept files are only asked about again with `--retry-kept`. The two go well together.
- **Start over:** delete that pair's `index.sqlite` before running sorto again. Every file is then analyzed from scratch; this also drops the cached model answers. Leftover `index.sqlite-wal` / `-shm` files are cleaned up automatically, and `progress.jsonl` is kept as history.
- **A log of every run:** each run starts a new file, `runs/run-2026-10-01_10-26-05.log`, named after the time the run began. It lists every file the run handled: its name, what the analysis said, and where it went or why it stayed. New IDs and folders that moved as a whole are in it too, and the last line counts the outcomes. Just before it, **left in the source** lists what the run leaves behind and why: duplicates and junk kept earlier (with the option that removes them), files left for you (`--retry-kept`), files with an error (`sorto resume`), and the git repositories and software packages it does not enter, with their file counts. A run that handled no files thus says why. The same list is printed at the end of the run. Files that were no longer in the source when their turn came are counted on one line, not as errors. The path is printed when the run starts and `sorto status` shows the latest one. A dry run writes one as well, with `would go to`. sorto never deletes these logs.

  ```
  sorto run started 2026-10-01 10:26:05
  source:      /home/me/Downloads
  target:      /home/me/Archive
  model:       qwen3.6:35b-a3b
  mode:        files are moved

  [2026-10-01 10:26:26] scan-0042.pdf  (212.4 KB, application/pdf)
      analysis:    invoice, confidence 0.95 → 13.13 Invoices
                   An electricity invoice from March 2025.
      why:         a bill addressed to the user
      moved to:    10-19 Life/13 Money/13.13 Invoices/2025/scan-0042.pdf

  [2026-10-01 10:26:41] notes.txt  (1.1 KB, text/plain)
      analysis:    note, confidence 0.40 → 11.12 Records
                   A short note without a clear subject.
      kept:        confidence 0.40 below 0.50; left in source

  sorto run ended 2026-10-01 10:31:02: 1 moved, 1 kept
  ```
- **Older versions** kept this in `~/.local/state/sorto/`. A pair's directory is moved to `~/.sorto/` the first time you use that pair again.

## Configuration

`~/.config/sorto/config.toml` holds user defaults (`sorto config` creates a commented template). For one source/target pair you can also put a `config.toml` into that pair's directory under `~/.sorto/` (shown by `sorto doctor`). Precedence, lowest to highest:

1. built-in defaults
2. Grok Build profile
3. user config
4. pair config
5. `SORTO_LLM_URL` / `SORTO_LLM_MODEL`
6. CLI flags

To change what the model is told, copy `src/sorto/prompts/classify.md` to `~/.config/sorto/classify.md` and edit it. Without that file the packaged prompt is used, so upgrades pick up prompt fixes.

## Screenshots

The README screenshot is real: sorto running in `xterm` inside a throwaway Debian 13 VM (QEMU/KVM, no shared folders), captured with ImageMagick. Only generic demo data is used: a fictional Johnny.Decimal tree, bills from "Northwind Energy" for "Alex Example", `example.com` addresses and a generated street-sign photo. The VM reaches the host's Ollama through an SSH reverse tunnel, so sorto still talks to its own `127.0.0.1`.

From the repository root:

```bash
docs/demo/vm.sh create                     # once: Debian cloud image + cloud-init seed in ~/.cache/sorto-demo-vm
SORTO_ARGS='--llm-model qwen3.5:9b-16k' docs/demo/vm.sh capture inbox
docs/demo/vm.sh stop
```

[`make-demo.sh`](demo/make-demo.sh) builds the demo data inside the VM, and [`capture.sh`](demo/capture.sh) drives the TUI and takes one frame per file (scenarios: `inbox`, `reorg`, `doctor`). Copy the frame you want to `docs/screenshots/sort-analysis.png`.

## Development

```bash
make install
make test
make lint
make compile   # sdist + wheel in dist/
```

Tests use a fake LLM and a temporary Johnny.Decimal tree. They cover:

- JD discovery, JDex notes and subfolder rules
- no overwrites, collision names and rename rules
- dry run, resume, and junk and duplicate handling
- git-repo protection and nested source/target
- the loopback-only guard and Grok profile import
- whole folders, date folders for own photos and videos, and the place of a GPS position
- new IDs and categories, the structure model, run logs and the TUI history

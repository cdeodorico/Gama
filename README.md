# Gama

<p align="center">
    <img width="1662" height="826" alt="Screenshot 2026-07-22 at 22-40-48 Gama" src="https://github.com/user-attachments/assets/227e3483-681a-4df4-b2c3-e3767dffb66d" />
</p>

A viewer for SR Research's EyeLink `.EDF` files. Through the use of SR Research's supplied `edfapi` library, Gama converts a recording the same way `edf2asc` does, then shows the result in the browser as a sortable, filterable table.

## Why

In our lab, the usual loop was: run `edf2asc` and convert the `.EDF` to a readable `.ASC`, open that `.ASC` in a text editor or Excel, then analyse through macros or other means. The excel import function that we became accustomed to tended to not respect column contexts, most likely because the converter didn't either. Additionally, if one was not interested in using the Experiment Viewer paid software, extra lines were included that could cloud data. Gama does the conversion and gives readable, filterable data immediately. Gama was built to replace the latter half of our labs pipeline rather than the actual decoding (I couldn't figure it out).

Gama uses the real `edfapi` through ctypes (accessed through the `eyelinkio` python package), and the output is byte-for-byte identical to `edf2asc` (if you output `.ASC`) on the files I've tested. If you export everything with no filters you get exactly the file `edf2asc` would have produced.

## Requirements

- Python 3.8+
- `eyelinkio`

```
pip install eyelinkio
```

Windows works perfectly, MacOS is being actively worked on, and I haven't tested linux compatibility yet.

## Running it

```
python gama.py
```

Gama starts a local server, opens your browser, and you pick files from there. The server binds to 127.0.0.1.

You can also pass files headless if you like:

```
python gama.py sub01.EDF sub02.EDF
```

Each file gets a tab. `+` adds more, `✕` closes one. Files are parsed (lazily) the first time you look at them, so as conserve resources. You also have the option of selection "Watch Folder", allowing the selection of a folder rather than a number of recordings. If any new `.EDF` files are added to this directory, Gama will detect them and automatically add them (again, lazily).

Filters live in the left panel and apply to all tab you've loaded, so you can set up a view configuration once and click between recordings to compare. There's an About/Help button at the bottom of that panel explaining every option and column.

## Command line

I don't ever use this but its easy to implement so have fun. Some examples:

```
# what's actually in this file?
python gama.py sub01.EDF --stats

# events only, as ASC
python gama.py sub01.EDF --export events.asc --only FIX,SACC,BLINK

# just my experiment messages, as CSV, times relative to the start of the recording
python gama.py sub01.EDF --export trials.csv --only MSG --msg-kinds experiment \
    --contains TRIAL_ --relative

# every file in the folder -> one CSV each
python gama.py *.EDF --export out/ --format csv --only FIX --min-fix-dur 100
```

With one input `--export` is a filename; with several it's a folder and you get
`<name>_filtered.<ext>` per recording.

Filters:

| flag | what it does |
| --- | --- |
| `--only` / `--hide` | categories: `PREAMBLE HEADER END INPUT MSG FIX SACC BLINK` |
| `--msg-kinds` | `experiment`, `config`, `cal`, `draw` |
| `--contains` / `--exclude` | match on the message body |
| `--search` | match on the raw line, any type |
| `--regex` | treat the three above as regexes |
| `--eye` | `R` or `L` |
| `--tmin` / `--tmax` | tracker time window |
| `--min-fix-dur` / `--min-sacc-dur` | ms, applied to EFIX / ESACC respectively |
| `--relative` | CSV/TSV times relative to each file's first timestamp |

`.ASC` exports always keep absolute timestamps, otherwise they wouldn't be valid ASC (sorry, just use `.CSV`).

## Presets

Save a set of filters from the sidebar in `presets/` next to the script/executable as a `.JSON` file.

## Trials and areas of interest

With the addition of trials analysis functionality, we can now do preliminary assessments that make finding essential data points easier. `Trials` in the toolbar opens a panel that cuts the recording into defined trials and works out which stimulus each fixation and saccade landed on (Please check data before interpreting, this is a perpetual beta feature).

It reads experimental messages. I'm not going to assume everyone labels things the way I do, so when the panel opens it scans the file, guesses the markers, and you can correct whatever it got wrong. On my recordings, it picks the lot unassisted: `TRIAL_START`, `TRIAL_END`, `STIM_POS`, and `DISPLAY_ONSET` -> `RESPONSE` for the analysis window. Your mileage may vary, so please be careful and double check your output.

### What you point it at

| | |
| --- | --- |
| **Trial start / end** | The messages that open and close a trial. Whatever trails the marker becomes per-trial variables, so `TRIAL_START index=1 block=6_True type=Relational Distractor` hands you `index`, `block` and `type` columns. |
| **Extra variables** | Other messages inside the trial worth harvesting - a metadata line, or the result line with accuracy and RT on it. Add as many as you want. |
| **Window** | Optionally only count events between two messages inside the trial (display onset -> response, say), with ms offsets if you need to nudge either edge. |
| **Stimuli / AOIs** | The message that places each stimulus, and which of its fields hold X, Y and the label. |
| **Region** | How big an AOI actually is: a circle of some radius (the default), a rectangle, nearest-stimulus-wins, or sizes read from fields in the message itself. |

### Message formats

Three ways to read fields:

- **`key=value`** - covers most things. There's a "values may contain spaces" toggle for when a value runs on, e.g. `type=Relational Distractor`, which otherwise gets culled at the space.
- **positions** - split on whitespace and pick fields by index, for `STIM 2 NT R 960 90` style lines.
- **regex** - named groups. For when your format is genuinely cursed.

`STIM_POS index=1 stim1 kind=NT dir=R x=960 y=90` parses fine as key=value with the spaces toggle *off* - the bare `stim1` becomes a positional field you can use as the label if you'd rather have that than `kind`.

### What you get out

**Preview** shows the first eight of parsed trials with their variables and AOI positions, use this to assess your config.

**Apply to table** adds `Trial` and `AOI` columns to the main view and CSV export. Your per-trial variables come with them: one column each, so `index`, `block` and `type` sit on every line of that trial and filter like anything else (numeric ones get `>` and `<=`, text ones match as text)

Two more things wake up once trials exist. **Timestamps** in the sidebar gains *relative to trial start* and *relative to window onset*, which is usually what you want when comparing latencies across trials: `Start` and `End` then read as ms from that trial's own zero, the time range boxes take the same units, and an export made in that mode carries the same numbers (the sidecar records which). Lines outside every trial have nothing to subtract from, so they come out blank. The **Line / Trial** dropdown switches it from line numbers to trial numbers.

**Export trials** One row per trial, with your variables plus fixation and saccade counts, the first fixation and first saccade, their latencies, and dwell time and fixation count per AOI label.

## Updates

At startup, Gama has a look at the GitHub releases page and tells you if there's a newer version.

If there's something new you get a small `Update: x.y.z` pill in the header; clicking it takes you to the download. There's also a line in About/Help with a **Check now** button and a tickbox to turn off automatic updates if you'd rather it didn't.

Gama determines how it is running, so the link differs: the `.exe` sends you to the release asset, a source checkout gets told to `git pull`. Outputs and sidecars aren't affected.

From the command line:

```
python gama.py --check-update      # ask now, print the answer, exit
python gama.py --no-update-check   # turn it off (remembered)
```

If GitHub is unreachable, rate limited, or there are no releases published yet, nothing is done.

# Keyboard shortcuts

`Ctrl` is `⌘` on macOS. Press `?` inside gama to see this list without leaving the app.

## Command palette

| Key | Action |
| --- | --- |
| `Ctrl` + `K` | Open the command palette. Fuzzy-search actions and presets, run with `Enter` |

## Finding things

| Key | Action |
| --- | --- |
| `Ctrl` + `F` | Jump to the search box |
| `Enter` | Next match (highlight mode) |
| `Shift` + `Enter` | Previous match |
| `F3` / `Shift` + `F3` | Next / previous match, from anywhere |
| `Ctrl` + `G` | Jump to the "Go to #" box — type a line number, `Enter` to go (flip the dropdown to `Trial` to jump by trial instead) |

## Moving around the table

| Key | Action |
| --- | --- |
| `↑` / `↓` | Up / down one row |
| `PgUp` / `PgDn` | Up / down one screen |
| `Home` / `End` | First / last row |
| `Shift` + any of the above | Extend the selection as you move |

## Selecting and copying

| Key | Action |
| --- | --- |
| Click | Select a row |
| `Shift` + click | Select a range |
| `Ctrl` + click | Add or remove a single row |
| `Ctrl` + `A` | Select everything in the current view |
| `Ctrl` + `C` | Copy the selection as TSV — paste straight into Excel |

With nothing selected, `Ctrl` + `C` copies the whole view. Only the visible columns
are copied, so hide what you don't want first via filters, column logical control, etc..

## Files and tabs

| Key | Action |
| --- | --- |
| `Ctrl` + `O` | Open a folder of recordings |
| `Alt` + `1` … `Alt` + `9` | Switch to tab 1–9 |
| `Alt` + `W` | Close the current tab |
| Double-click a tab | Rename it |

## Everything else

| Key | Action |
| --- | --- |
| `Ctrl` + `E` | Open the export menu |
| `?` | Open About / Help |
| `Esc` | Close the open dialog |

## Notes and limitations

- Events only. Samples aren't loaded, can't really do that without breaching copyright (I think...).
- The first line of an ASC (`** CONVERTED FROM ...`) records the path, edfapi version and time of the *original* conversion. None of that is in the EDF, so it's a default string you can override with `--converted-from-line`.
- Blinks inside a saccade get merged into one, and missing gaze shows up as `.` with a huge scientific-notation amplitude. Both are expected `edf2asc` behaviours, reproduced, much to my own chagrin.
- Trial and AOI matching is only as good as the radius you hand it. Check the preview and the rank columns before you trust a spreadsheet (coming from experience).
- Builds are Windows-only for now. Nothing in here is platform-specific - `edfapi` comes from `eyelinkio`, which ships the library for each platform - so a macOS build is a packaging job: a `.icns` alongside `icon.ico`, a `.app` bundle, and the zipped bundle attached to the release (the updater already looks for `.dmg`, `.pkg` or `.zip` on a Mac). Until then a Mac needs `python gama.py` (I'm working on it right now).

## License

GNU GPLv3.

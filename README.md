# Gama

<p align="center">
    <img width="1662" height="826" alt="Screenshot 2026-07-22 at 22-40-48 Gama" src="https://github.com/user-attachments/assets/227e3483-681a-4df4-b2c3-e3767dffb66d" />
</p>

A viewer for SR Research EyeLink `.EDF` files. It converts a recording the same way SR Research's `edf2asc` does, then shows the result in the browser as a sortable, filterable table instead of an insane wall of text.

Through multiple filters one can extract what they actually care about. If you don't want all the dataviewer BS, just filter it.

## Why

The usual loop is: run `edf2asc`, open the `.asc` in a text editor or Excel, then good luck. The excel import function molests your file, the columns don't line up because every line type has a different "shape" , and 45% of the file is `!CAL` and `!V` lines you don't care about. This does the conversion in memory and gives you filters instead. Thank me in 20 years.

The conversion is not a re-implementation (I'd kill myself before it was) - it drives the real `edfapi` through ctypes, and the output is byte-for-byte identical to `edf2asc` (if you output `.asc`) on the files I've tested (same md5). If you export everything with no filters you get exactly the file `edf2asc` would have produced, so nothing downstream needs to change.

## Requirements

- Python 3.8+
- `eyelinkio` (this is what supplies `edfapi` itself)

```
pip install eyelinkio
```

Windows, macOS and Linux all work (as far as I know).

## Running it

```
python gama.py
```

That's it. Gama starts a small local server, opens your browser, and you pick files from there. The server binds to 127.0.0.1 and only talks to your own machine.

You can also pass files headless if you like:

```
python gama.py sub01.EDF sub02.EDF
```

Each file gets a tab. `+` adds more, `✕` closes one. Files are parsed (lazily) the first time you look at them, so opening ten recordings doesn't crash shit systems until you click through to them. You also have the option of selection "Watch Folder". This allows the selection of a folder rather than a number of recordings. If any new `.EDF` files are added to this directory, Gama will detect them and automatically add them (lazily).

Filters live in the left panel and apply to whichever tab you're on, so you can set up a view configuration once and click between recordings to compare. There's an About/Help button at the bottom of that panel explaining every option and column.
## Command line

I don't ever use this but its easy to implement so have fun. Some examples:

```
# what's actually in this file?
python gama.py sub01.EDF --stats

# events only, as ASC
python gama.py sub01.EDF --export events.asc --only FIX,SACC,BLINK

# just my trial messages, as CSV, times relative to the start of the recording
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

ASC exports always keep absolute timestamps, otherwise they wouldn't be valid ASC (sorry, just use CSV).

## Presets

Save a set of filters from the sidebar in `presets/` next to the script as a small JSON file. They're  text and one file per preset, so you can commit them, email them, or hand-edit them, etc..

## Trials and areas of interest

The other half of the program, and the bit that turns this from a viewer into something that produces numbers. `Trials ▾` in the toolbar opens a panel that cuts the recording into trials and works out which stimulus each fixation and saccade actually landed on.

It reads *your* messages. I'm not going to pretend everyone labels things the way I do, so when the panel opens it scans the file, guesses the markers, and you correct whatever it got wrong. On my recordings it picks the lot unassisted: `TRIAL_START`, `TRIAL_END`, `STIM_POS`, and `DISPLAY_ONSET` -> `RESPONSE` for the analysis window.

### What you point it at

| | |
| --- | --- |
| **Trial start / end** | The messages that open and close a trial. Whatever trails the marker becomes per-trial variables, so `TRIAL_START index=1 block=6_True type=Relational Distractor` hands you `index`, `block` and `type` columns. |
| **Extra variables** | Other messages inside the trial worth harvesting - a metadata line, or the result line with accuracy and RT on it. Add as many as you want, they all get folded into the trial. |
| **Window** | Optionally only count events between two messages inside the trial (display onset -> response, say), with ms offsets if you need to nudge either edge. |
| **Stimuli / AOIs** | The message that places each stimulus, and which of its fields hold X, Y and the label. |
| **Region** | How big an AOI actually is: a circle of some radius (the default), a rectangle, nearest-stimulus-wins, or sizes read from fields in the message itself. |

### Message formats

Three ways to read fields, because nobody formats these the same:

- **`key=value`** - covers most things. There's a "values may contain spaces" toggle for when a value runs on, e.g. `type=Relational Distractor`, which otherwise gets guillotined at the space.
- **positions** - split on whitespace and pick fields by index, for `STIM 2 NT R 960 90` style lines.
- **regex** - named groups. The escape hatch for when your format is genuinely cursed.

`STIM_POS index=1 stim1 kind=NT dir=R x=960 y=90` parses fine as key=value with the spaces toggle *off* - the bare `stim1` becomes a positional field you can use as the label if you'd rather have that than `kind`.

### What you get out

**Preview** shows the first handful of parsed trials with their variables and AOI positions, so you find out you've cocked up the config before you commit to it rather than after.

**Apply to table** adds `Trial` and `AOI` columns to the main view - filterable and sortable like everything else, and they ride along in the CSV/TSV/HTML row exports (saccades also carry the AOI they left from).

**Export trials** is the one you actually want: one row per trial, with your variables plus fixation and saccade counts, the first fixation and first saccade, their latencies, and dwell time and fixation count per AOI label.

"First" means the first one that *hit something*. The opening fixation is usually still parked in the middle of the screen and reporting that as your first fixation is useless, so anything that landed on nothing gets skipped. `first_fix_rank` / `first_sacc_rank` tell you how many were skipped (1 = the very first one hit an AOI), which is a decent smell test - if that number starts creeping up, your radius is probably too small.

Save the whole setup as a **scheme** and it lands in `schemes/` next to the script. Configure it once, use it on every participant, email it to whoever's stuck doing the analysis.

This bit is UI only, there's no command line equivalent yet.

# Keyboard shortcuts

`Ctrl` is `⌘` on macOS. Press `?` inside gama to see this list without leaving the app.

## Command palette

| Key | Action |
| --- | --- |
| `Ctrl` + `K` | Open the command palette — fuzzy-search every action and preset, run with `Enter` |

## Finding things

| Key | Action |
| --- | --- |
| `Ctrl` + `F` | Jump to the search box |
| `Enter` | Next match (highlight mode) |
| `Shift` + `Enter` | Previous match |
| `F3` / `Shift` + `F3` | Next / previous match, from anywhere |
| `Ctrl` + `G` | Jump to the "Go to #" box — type a line number, `Enter` to go |

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
are copied, so hide what you don't want first.

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

- Events only. Samples aren't loaded, which is why it's fast and why there's no
  gaze trace. If you need samples, use `edf2asc` proper (sorry).
- The first line of an ASC (`** CONVERTED FROM ...`) records the path, edfapi
  version and time of the *original* conversion. None of that is in the EDF, so
  it's a default string you can override with `--converted-from-line`.
- Blinks inside a saccade get merged into one, and missing gaze shows up as `.`
  with a huge scientific-notation amplitude. Both are `edf2asc` behaviours, faithfully
  reproduced, much to my chagrin.
- Trial and AOI matching is only as good as the radius you hand it. Check the
  preview and the rank columns before you trust a spreadsheet.
- `gama.py` is only the launcher. The code lives in `gamalib/` next to it, a
  module per job - `convert.py` does EDF->ASC, `trials.py` the trial and AOI
  analysis, `server.py` the web bits, and so on. Keep those together along with
  `index.html`. If you ever touch `convert.py`, check byte-identity still holds
  before anything else:

  ```
  python gama.py rec.EDF --export /tmp/out.asc
  cmp /tmp/out.asc reference.asc
  ```

## License

GNU GPLv3.

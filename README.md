# Gama

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

Each file gets a tab. `+` adds more, `✕` closes one. Files are parsed (lazily) the first time you look at them, so opening ten recordings doesn't crash shit systems until you click through to them.

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

## Notes and limitations

- Events only. Samples aren't loaded, which is why it's fast and why there's no
  gaze trace. If you need samples, use `edf2asc` proper (sorry).
- The first line of an ASC (`** CONVERTED FROM ...`) records the path, edfapi
  version and time of the *original* conversion. None of that is in the EDF, so
  it's a default string you can override with `--converted-from-line`.
- Blinks inside a saccade get merged into one, and missing gaze shows up as `.`
  with a huge scientific-notation amplitude. Both are `edf2asc` behaviours, faithfully
  reproduced, much to my chagrin.
- Everything is in one file (`gama.py`), engine and UI, so there's nothing to
  install and nothing to keep in sync.

## License

GNU GPLv3.

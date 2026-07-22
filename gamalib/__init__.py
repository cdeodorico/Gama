"""gama - an explorer for SR Research EyeLink .EDF recordings.

The package is split so each piece can be read on its own:

    edfapi       loading the native SR library
    convert      EDF -> ASC, byte-identical to edf2asc
    dataset      converted records -> rows and columns
    filters      row filtering shared by the UI and the CLI
    exports      ASC / CSV / TSV / HTML output
    trials       trial segmentation and AOI matching
    notes        per-row flags and notes
    preset_store saved filter presets and trial schemes
    files        open files, the tab registry, folder watching
    paths        where things live; bundled resources
    diagnostics  the support/bug-report payload
    server       the local HTTP server and JSON API
    cli          argument parsing and the entry point
"""

from .version import __version__

__all__ = ["__version__"]

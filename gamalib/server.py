"""The local HTTP server and its JSON API."""

import gzip
import io
import json
import os
import threading
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from urllib.parse import urlparse, parse_qs

from .version import __version__
from .paths import ICON_BYTES, html_page
from .filters import filter_indices
from .exports import (export_bytes, _opts_from_json, _provenance_bytes,
                      _esc, _HTML_DOC)
from .notes import save_notes, _notes_path
from .preset_store import (_load_presets, _save_preset, _delete_preset,
                      _schemes_dir)
from .files import (Registry, Watcher, _ensure_parsed,
                    browse_dir, list_edfs, _is_edf)
from .trials import suggest_markers, analyse_trials
from .diagnostics import diagnostics



def make_handler(reg, converted_from_line, presets_dir, watcher):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype, headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        def _body(self):
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")

        def _file_arg(self):
            q = parse_qs(urlparse(self.path).query)
            try:
                return int(q.get("file", ["0"])[0])
            except ValueError:
                return 0

        def do_GET(self):
            route = urlparse(self.path).path
            if route == "/":
                self._send(200, html_page().encode("utf-8"),
                           "text/html; charset=utf-8")
            elif route == "/icon.png" or route == "/favicon.ico":
                if ICON_BYTES:
                    self._send(200, ICON_BYTES, "image/png",
                               {"Cache-Control": "max-age=86400"})
                else:
                    self._send(404, b"", "text/plain")
            elif route == "/api/info":
                self._json(diagnostics(presets_dir))
            elif route == "/api/files":
                self._json(reg.listing())
            elif route == "/api/browse":
                q = parse_qs(urlparse(self.path).query)
                self._json(browse_dir(q.get("path", [""])[0]))
            elif route == "/api/watch":
                self._json(watcher.status())
            elif route == "/api/schemes":
                self._json(_load_presets(_schemes_dir(presets_dir)))
            elif route == "/api/presets":
                self._json(_load_presets(presets_dir))
            elif route == "/api/progress":
                entry = reg.get(self._file_arg())
                if entry is None:
                    self._json({"state": "gone"})
                elif entry.error:
                    self._json({"state": "error"})
                elif entry.ready:
                    self._json({"state": "ready", "seen": entry.progress})
                else:
                    # read the plain int without taking entry.lock, which the
                    # parse thread holds for its whole duration
                    self._json({"state": "parsing" if entry.parsing else "queued",
                                "seen": entry.progress})
            elif route == "/api/notes":
                entry = reg.get(self._file_arg())
                if entry is None:
                    self._send(404, b"file not open", "text/plain")
                    return
                _ensure_parsed(entry, converted_from_line)
                self._json({"notes": entry.notes})
            elif route == "/api/rows":
                entry = reg.get(self._file_arg())
                if entry is None:
                    self._send(404, b"file not open", "text/plain")
                    return
                _ensure_parsed(entry, converted_from_line)
                if entry.error:
                    self._send(500, ("Failed to parse " + entry.name + ":\n\n"
                                     + entry.error).encode("utf-8"),
                               "text/plain; charset=utf-8")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(entry.payload_gz)))
                self.end_headers()
                self.wfile.write(entry.payload_gz)
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            route = urlparse(self.path).path
            if route == "/api/export":
                self._export_one()
            elif route == "/api/export_all":
                self._export_all()
            elif route == "/api/presets":
                self._presets_write()
            elif route == "/api/open":
                self._open_files()
            elif route == "/api/open_folder":
                self._open_folder()
            elif route == "/api/watch":
                self._watch()
            elif route == "/api/close":
                self._close_file()
            elif route == "/api/notes":
                self._write_note()
            elif route == "/api/schemes":
                self._schemes_write()
            elif route == "/api/trials/suggest":
                self._trials_suggest()
            elif route == "/api/trials/run":
                self._trials_run()
            elif route == "/api/trials/export":
                self._trials_export()
            else:
                self._send(404, b"not found", "text/plain")

        # ---- trial / AOI analysis -----------------------------------
        def _entry_for(self, req):
            entry = reg.get(req.get("file"))
            if entry is None:
                self._send(404, b"file not open", "text/plain")
                return None
            _ensure_parsed(entry, converted_from_line)
            if entry.error:
                self._send(500, entry.error.encode("utf-8"), "text/plain")
                return None
            return entry

        def _trials_suggest(self):
            req = self._body()
            entry = self._entry_for(req)
            if entry is None:
                return
            self._json(suggest_markers(entry.rows, entry.parsed))

        def _trials_run(self):
            req = self._body()
            entry = self._entry_for(req)
            if entry is None:
                return
            res = analyse_trials(entry.rows, entry.parsed, req.get("scheme") or {},
                                 req.get("limit"))
            if not req.get("preview") and "row_trial" in res:
                entry.trials = {"row_trial": res["row_trial"],
                                "row_aoi": res["row_aoi"],
                                "row_aoi_from": res["row_aoi_from"]}
            if req.get("preview"):
                res.pop("row_trial", None)
                res.pop("row_aoi", None)
                res.pop("row_aoi_from", None)
                res["summary"]["rows"] = res["summary"]["rows"][:25]
            body = gzip.compress(json.dumps(res).encode("utf-8"), 5)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _trials_export(self):
            req = self._body()
            entry = self._entry_for(req)
            if entry is None:
                return
            res = analyse_trials(entry.rows, entry.parsed, req.get("scheme") or {})
            fmt = req.get("format", "csv")
            cols, srows = res["summary"]["columns"], res["summary"]["rows"]
            if fmt == "html":
                head = "".join("<th>%s</th>" % _esc(c) for c in cols)
                body_rows = "\n".join(
                    "<tr>" + "".join("<td>%s</td>" % _esc(v) for v in r) + "</tr>"
                    for r in srows)
                sub = ("%d trials &middot; exported by gama %s"
                       % (len(srows), __version__))
                body = _HTML_DOC.format(title=_esc(entry.name + " — trials"),
                                        sub=sub, head=head,
                                        rows=body_rows).encode("utf-8")
                ext = ".html"
            else:
                import csv as _csv
                buf = StringIO()
                w = _csv.writer(buf, delimiter=("\t" if fmt == "tsv" else ","),
                                lineterminator="\n")
                w.writerow(cols)
                for r in srows:
                    w.writerow(r)
                body = buf.getvalue().encode("utf-8")
                ext = ".tsv" if fmt == "tsv" else ".csv"
            self._send(200, body, "application/octet-stream",
                       {"Content-Disposition":
                        'attachment; filename="%s_trials%s"' % (entry.base, ext)})

        def _schemes_write(self):
            req = self._body()
            d = _schemes_dir(presets_dir)
            name = (req.get("name") or "").strip()
            if req.get("action") == "delete":
                _delete_preset(d, name)
            elif name:
                _save_preset(d, name, req.get("scheme") or {})
            self._json(_load_presets(d))

        def _write_note(self):
            req = self._body()
            entry = reg.get(req.get("file"))
            if entry is None:
                self._send(404, b"file not open", "text/plain")
                return
            _ensure_parsed(entry, converted_from_line)
            line = str(req.get("line"))
            flag = bool(req.get("flag"))
            note = str(req.get("note") or "").strip()
            with entry.lock:
                if flag or note:
                    entry.notes[line] = {"flag": flag, "note": note}
                else:
                    entry.notes.pop(line, None)
                entry.notes = save_notes(entry.path, entry.notes)
            self._json({"notes": entry.notes,
                        "path": _notes_path(entry.path)})

        def _open_files(self):
            req = self._body()
            added, skipped = [], []
            for p in req.get("paths", []):
                if os.path.isfile(p) and _is_edf(p):
                    added.append(reg.add(p))
                else:
                    skipped.append(p)
            self._json({"files": reg.listing(), "added": added,
                        "skipped": skipped})

        def _open_folder(self):
            req = self._body()
            folder = req.get("path") or ""
            recursive = bool(req.get("recursive"))
            paths = list_edfs(folder, recursive)
            added = [reg.add(p) for p in paths]
            self._json({"files": reg.listing(), "added": added,
                        "folder": os.path.abspath(folder) if folder else None,
                        "count": len(added)})

        def _watch(self):
            req = self._body()
            action = req.get("action", "start")
            if action == "stop":
                watcher.stop()
                self._json({"files": reg.listing(), "watch": watcher.status()})
                return
            folder = req.get("path") or ""
            recursive = bool(req.get("recursive"))
            # Watching a folder starts a clean session, so anything already open
            # is closed first unless the caller says otherwise.
            if req.get("clear", True):
                reg.clear()
            added = []
            if req.get("open_existing", True):
                added = [reg.add(p) for p in list_edfs(folder, recursive)]
            watcher.start(folder, recursive)
            self._json({"files": reg.listing(), "added": added,
                        "watch": watcher.status()})

        def _close_file(self):
            req = self._body()
            reg.close(req.get("file"))
            self._json({"files": reg.listing()})

        def _export_one(self):
            req = self._body()
            entry = reg.get(req.get("file"))
            if entry is None:
                self._send(404, b"file not open", "text/plain")
                return
            _ensure_parsed(entry, converted_from_line)
            if entry.error:
                self._send(500, entry.error.encode("utf-8"), "text/plain")
                return
            fmt = req.get("format", "asc")
            relative = req.get("relative", False)
            indices = req.get("indices", [])
            body, ext = export_bytes(entry, indices, fmt, relative)
            # A provenance sidecar is offered for data exports (never ASC).  Since
            # a browser download is a single file, we bundle export + sidecar in a
            # small ZIP when it's requested.
            if req.get("sidecar") and fmt != "asc":
                prov = _provenance_bytes(entry, fmt, req.get("opts", {}),
                                         relative, len(indices))
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    z.writestr(f"{entry.base}_filtered{ext}", body)
                    z.writestr(f"{entry.base}_filtered.gama.json", prov)
                self._send(200, buf.getvalue(), "application/zip",
                           {"Content-Disposition":
                            f'attachment; filename="{entry.base}_filtered.zip"'})
                return
            self._send(200, body, "application/octet-stream",
                       {"Content-Disposition":
                        f'attachment; filename="{entry.base}_filtered{ext}"'})

        def _export_all(self):
            req = self._body()
            fmt = req.get("format", "asc")
            relative = req.get("relative", False)
            sidecar = bool(req.get("sidecar")) and fmt != "asc"
            opts_json = req.get("opts", {})
            opts = _opts_from_json(opts_json)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for entry in reg.entries():
                    _ensure_parsed(entry, converted_from_line)
                    if entry.error:
                        continue
                    idx = filter_indices(entry.rows, entry.parsed, opts)
                    body, ext = export_bytes(entry, idx, fmt, relative)
                    z.writestr(f"{entry.base}_filtered{ext}", body)
                    if sidecar:
                        z.writestr(f"{entry.base}_filtered.gama.json",
                                   _provenance_bytes(entry, fmt, opts_json,
                                                     relative, len(idx)))
            data = buf.getvalue()
            self._send(200, data, "application/zip",
                       {"Content-Disposition":
                        f'attachment; filename="edf_export_{fmt}.zip"'})

        def _presets_write(self):
            req = self._body()
            action = req.get("action")
            name = (req.get("name") or "").strip()
            if action == "save" and name:
                _save_preset(presets_dir, name, req.get("config", {}))
            elif action == "delete" and name:
                _delete_preset(presets_dir, name)
            self._json(_load_presets(presets_dir))
    return Handler



def serve(paths, converted_from_line, port, open_browser, presets_dir):
    reg = Registry()
    for p in paths:
        reg.add(p)
    watcher = Watcher(reg)
    try:
        os.makedirs(presets_dir, exist_ok=True)
    except OSError:
        pass
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", port),
        make_handler(reg, converted_from_line, presets_dir, watcher))
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    n = len(paths)
    print(f"EDF Explorer: {n} file(s) preloaded"
          if n else "EDF Explorer: add files from the browser (+ tab)")
    print(f"Presets folder: {presets_dir}")
    print(f"Running at {url}\nPress Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()
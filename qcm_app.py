"""
QCM Scanner -- local windowed application to generate answer sheets and
grade scanned copies.

This file is the only new piece that builds an interface around the 4
already-written and tested modules (generate_sheet_modular, detect_modular,
roster_match, sheet_layout_v2): it does not reimplement sheet drawing nor
optical reading, only the form, the results list, grading (scoring.py)
and the translated user interface (translations.py).
"""
import csv
import glob
import math
import os
import sys
import tempfile
import threading
import time
import traceback
import tkinter as tk
from dataclasses import replace as dataclass_replace
from tkinter import ttk, filedialog, messagebox, simpledialog

from PIL import Image, ImageTk

from generate_sheet_modular import (
    SheetConfig, build, build_batch, draw_sheet, needs_recto_verso, MAX_QUESTIONS,
)
from roster_match import load_roster, process_batch, format_batch_report
import scoring
import app_config
import class_archive
import answer_key_store
import translations
from translations import tr

APP_TITLE = "QCM Scanner"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")

# Stable (language-independent) ids for the sheet-size presets. The
# Combobox shows the translated label (tr(f"preset_{id}")); only the id
# is ever persisted to settings, so switching language never breaks a
# previously saved choice.
SIZE_PRESET_IDS = ["small", "medium", "large"]
SIZE_PRESET_PARAMS = {
    "small": dict(scale=1.0, tiles_rows=2, tiles_cols=2),
    "medium": dict(scale=2 ** 0.5, tiles_rows=1, tiles_cols=2),
    "large": dict(scale=2.0, tiles_rows=1, tiles_cols=1),
}

STATUS_KEY_MAP = {
    "ok": "status_ok",
    "feuille_douteuse": "status_douteuse",
    "numero_inconnu": "status_inconnu",
    "face_manquante": "status_manquante",
    "erreur_lecture": "status_erreur",
}


def status_label(status):
    return tr(STATUS_KEY_MAP.get(status, status))


def _fmt_points(x):
    """Formats a point value without a trailing ".0" for a whole number
    (e.g. 1.0 -> "1", 1.5 -> "1.5"), for display in the barème grid and
    in fraction-mode grades."""
    x = float(x)
    return str(int(x)) if x == int(x) else f"{x:g}"


def format_note(entry, out_of_20, include_uncertain_suffix=True):
    """Formats one entry's grade for display/export. Default is the raw
    fraction (points/max_points, e.g. "8/12") rather than a /20 grade --
    it shows exactly what was graded without implying every quiz is
    scaled to 20 points. Ticking "Grade out of 20" (ScanTab.
    note_out_of_20_var) switches to the equivalent note, still computed
    by scoring.compute_scores either way."""
    points = entry.get("points")
    max_points = entry.get("max_points")
    if points is None or max_points is None:
        return "-"
    if out_of_20:
        note = entry.get("note")
        text = f"{note:.2f}/20" if note is not None else "-"
    else:
        text = f"{_fmt_points(points)}/{_fmt_points(max_points)}"
    if include_uncertain_suffix and entry.get("incertaines"):
        text += f" (⚠ {len(entry['incertaines'])})"
    return text


def build_results_csv_rows(report, out_of_20):
    """Builds (headers, rows) for `report`, in the CSV format used both
    by the manual "Export results" button and by the automatic per-class
    archive (see class_archive.save_results) -- kept as a single shared
    function so the two never drift apart."""
    max_q = 0
    for e in report:
        if e.get("questions"):
            max_q = max(max_q, max(e["questions"].keys()))
    has_notes = any(e.get("points") is not None for e in report)
    headers = ["numero", "eleve", "classe", "statut"]
    if has_notes:
        headers += ["note", "questions_incertaines"]
    headers += [f"Q{q}" for q in range(1, max_q + 1)]
    rows = []
    for e in report:
        row = [e.get("sheet_number", ""), e.get("eleve", ""), e.get("classe", ""),
               status_label(e.get("status"))]
        if has_notes:
            row.append(format_note(e, out_of_20, include_uncertain_suffix=False))
            row.append(",".join(str(q) for q in e.get("incertaines", [])))
        questions = e.get("questions", {})
        for q in range(1, max_q + 1):
            qres = questions.get(q)
            row.append(";".join(qres["answers"]) if qres else "")
        rows.append(row)
    return headers, rows


def write_results_csv(path, headers, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(headers)
        w.writerows(rows)


# Statuses for which entry["questions"] is a COMPLETE merge (every side of
# the sheet was found -- only "face_manquante" and "erreur_lecture" have an
# incomplete or absent "questions" dict, see roster_match.process_batch).
_COMPLETE_QUESTIONS_STATUSES = {"ok", "feuille_douteuse", "numero_inconnu"}


def question_read_progress(entry):
    """Returns (n_read, n_total) for one result entry, or None if not
    applicable (missing side / read error, where the total isn't known).

    n_total: total number of questions on the sheet -- detect_modular.
    analyse() always produces one result row per question index decoded
    from the configuration barcode (whether that row was read cleanly or
    not), so len(entry["questions"]) already equals that barcode-encoded
    total once every side of the sheet has been merged in.

    n_read: number of those rows read WITHOUT any bubble flagged for
    manual review (a flagged bubble means no reliable separation was
    found for that particular question -- not necessarily a blank
    answer)."""
    if entry.get("status") not in _COMPLETE_QUESTIONS_STATUSES:
        return None
    questions = entry.get("questions")
    if not questions:
        return None
    n_total = len(questions)
    n_read = sum(1 for r in questions.values() if not r.get("flagged"))
    return n_read, n_total


HEADER_TOKENS = {"numero", "numéro", "nom", "classe"}


def _parse_tabular_rows(rows, col_index):
    """Builds a roster {numero: {nom, classe}} from rows already split
    into columns (one row = one student). col_index gives the index of
    each recognized column ('nom' required, 'numero' and 'classe'
    optional). Students without a number get the smallest free number,
    grouped by class (in the order classes first appear in the file)
    then alphabetically by name (nom/classe/numero are the field names
    used everywhere in the data model -- French for "name"/"class"/
    "number" -- kept as-is, see roster_match.py)."""
    entries = []
    for row in rows:
        def get(col):
            i = col_index.get(col)
            return row[i].strip() if (i is not None and i < len(row)) else ""

        nom = get("nom")
        if not nom:
            continue
        classe = get("classe")
        numero_str = get("numero")
        numero = int(numero_str) if numero_str.isdigit() else None
        entries.append({"numero": numero, "nom": nom, "classe": classe})

    if not entries:
        return {}

    reserved = {e["numero"] for e in entries if e["numero"] is not None}
    class_order = []
    for e in entries:
        if e["classe"] not in class_order:
            class_order.append(e["classe"])

    need_auto = [e for e in entries if e["numero"] is None]
    need_auto.sort(key=lambda e: (class_order.index(e["classe"]), e["nom"].lower()))

    next_num = 1
    for e in need_auto:
        while next_num in reserved:
            next_num += 1
        e["numero"] = next_num
        reserved.add(next_num)
        next_num += 1

    return {e["numero"]: {"nom": e["nom"], "classe": e["classe"]} for e in entries}


def _parse_wide_multi_classe(rows):
    """'Wide' format: one row = one class, first column = class name,
    following columns = names of the students in that class (e.g.
    class1;Alice;Bob then class2;Chloe;David;Eve on the next row).
    Numbers are assigned sequentially, class by class (in row order),
    alphabetically by name within each class."""
    roster = {}
    next_num = 1
    for row in rows:
        cells = [c.strip() for c in row if c.strip()]
        if not cells:
            continue
        classe, noms = cells[0], cells[1:]
        for nom in sorted(noms, key=str.lower):
            roster[next_num] = {"nom": nom, "classe": classe}
            next_num += 1
    return roster


def smart_load_roster(path):
    """Loads a student CSV tolerantly. First tries the standard
    numero;nom;classe format (roster_match.load_roster, already tested);
    if the 'numero' column is missing or incomplete, automatically
    detects either a nom[;classe] format (numbers assigned
    automatically), or the 'wide' one-row-per-class format.
    Returns (roster, message_or_None) -- message is not None if numbers
    were assigned automatically (the caller should then offer to save a
    standard-format CSV)."""
    try:
        roster = load_roster(path)
        if roster:
            return roster, None
    except (KeyError, ValueError):
        pass

    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [row for row in csv.reader(f, delimiter=";") if any(c.strip() for c in row)]
    if not rows:
        raise ValueError(tr("csv_empty"))

    header = [c.strip().lower() for c in rows[0]]
    looks_like_header = len(header) <= 3 and any(tok in HEADER_TOKENS for tok in header)

    auto_msg = tr("csv_auto_numbered")

    if looks_like_header:
        col_index = {}
        for i, name in enumerate(header):
            if name in ("numero", "numéro"):
                col_index["numero"] = i
            elif name == "nom":
                col_index["nom"] = i
            elif name == "classe":
                col_index["classe"] = i
        roster = _parse_tabular_rows(rows[1:], col_index)
    else:
        widths = [len(r) for r in rows]
        if max(widths) <= 2:
            col_index = {"nom": 0}
            if max(widths) == 2:
                col_index["classe"] = 1
            roster = _parse_tabular_rows(rows, col_index)
        else:
            roster = _parse_wide_multi_classe(rows)
            auto_msg = tr("csv_multi_class_detected")

    if not roster:
        raise ValueError(tr("csv_unrecognized_format"))
    return roster, auto_msg


def export_roster_csv(path, roster):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["numero", "nom", "classe"])
        for num in sorted(roster.keys()):
            w.writerow([num, roster[num]["nom"], roster[num].get("classe", "")])


def smart_load_roster_with_export(path):
    """smart_load_roster + automatically saves a standard-format CSV if
    numbers were assigned automatically. Returns (roster,
    path_to_use_next, message_or_None)."""
    roster, message = smart_load_roster(path)
    if message:
        base, _ext = os.path.splitext(path)
        export_path = base + "_numerote.csv"
        export_roster_csv(export_path, roster)
        message += tr("csv_standard_saved", path=export_path)
        return roster, export_path, message
    return roster, path, None


def suggest_class_name(roster):
    """Guesses a default class name for 'Save to memory': the most
    frequent 'classe' value in the roster, if any."""
    from collections import Counter
    classes = [info.get("classe", "").strip() for info in roster.values() if info.get("classe", "").strip()]
    if classes:
        return Counter(classes).most_common(1)[0][0]
    return ""


_UNSAFE_FILENAME_CHARS = '<>:"/\\|?*'


def _sanitize_filename_part(text):
    """Strips characters that Windows forbids in file/folder names, so a
    class name typed freely by the teacher (e.g. "6°A / SEGPA") can be
    used as part of an output folder/file name."""
    cleaned = "".join(ch for ch in text if ch not in _UNSAFE_FILENAME_CHARS).strip()
    return cleaned or "?"


def _parse_pdf_date(meta_str):
    """Parses a PDF metadata date string (e.g. "D:20260912153000+02'00'")
    into epoch seconds, or None if absent/unparseable."""
    if not meta_str or not meta_str.startswith("D:"):
        return None
    try:
        return time.mktime(time.strptime(meta_str[2:16], "%Y%m%d%H%M%S"))
    except ValueError:
        return None


def extract_scan_date(photo_paths):
    """Best-effort date the copies were scanned/photographed, read from
    the source files' own metadata rather than "now": EXIF
    DateTimeOriginal for a photo taken with a phone/camera, PDF creation
    date for a page extracted from a scanned PDF (see _add_pdf, which
    stamps the extracted page images' modification time with that same
    PDF date so this lookup works uniformly for both cases). Falls back
    to the file's modification time, then to the current time if nothing
    at all is available. Returns a time.struct_time."""
    for path in photo_paths:
        try:
            with Image.open(path) as img:
                exif = img.getexif()
                raw = exif.get(36867) or exif.get(306)  # DateTimeOriginal, DateTime
            if raw:
                return time.strptime(raw.strip(), "%Y:%m:%d %H:%M:%S")
        except (OSError, ValueError, SyntaxError):
            pass
    mtimes = [os.path.getmtime(p) for p in photo_paths if os.path.exists(p)]
    if mtimes:
        return time.localtime(min(mtimes))
    return time.localtime()


def dedupe_name(base, existing_names):
    """Appends "_2", "_3"... to `base` until it's not in `existing_names`,
    so two things named the same (two runs the same day, or a name the
    teacher typed by hand) don't collide/overwrite each other."""
    name = base
    n = 2
    while name in existing_names:
        name = f"{base}_{n}"
        n += 1
    return name


def suggest_run_classe(roster):
    """Class name to use for a correction run's archive: the roster's own
    class if there is one, otherwise a generic translated placeholder."""
    classe = suggest_class_name(roster) if roster else ""
    return _sanitize_filename_part(classe) if classe else tr("scan_run_name_no_class")


def build_run_name(roster, photo_paths, existing_names=()):
    """Suggested default name for a correction run's output folder (and
    the matching default export filename): class name + scan date, e.g.
    "Correction_6A_2026-09-15", instead of an opaque timestamp -- the
    teacher can freely edit this suggestion (see ScanTab._on_run)."""
    classe = suggest_run_classe(roster)
    date_str = time.strftime("%Y-%m-%d", extract_scan_date(photo_paths))
    base = f"{tr('scan_run_name_prefix')}_{classe}_{date_str}"
    return dedupe_name(base, existing_names)


# ---------------------------------------------------------------------
# Sheet generation for an ARBITRARY (not necessarily consecutive) list
# of numbers -- case of an already-existing class CSV where each student
# already has a number assigned. Reuses exactly the same tiling loop as
# build_batch (generate_sheet_modular.py), only the choice of numbers
# changes; the drawing itself (draw_sheet) is untouched.
# ---------------------------------------------------------------------
def build_for_numbers(path, cfg, numbers, identities=None):
    """identities: {numero (int): (nom, classe)} optional -- see
    generate_sheet_modular.build_batch."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm as MM

    cfg.validate()
    page_w, page_h = cfg.resolve_page_size()
    n_tiles = cfg.tiles_rows * cfg.tiles_cols
    rv = needs_recto_verso(cfg)
    c = canvas.Canvas(path, pagesize=(page_w * MM, page_h * MM))

    def draw_tile_page(page_numbers, side):
        for idx, num in enumerate(page_numbers):
            row = idx // cfg.tiles_cols
            col = idx % cfg.tiles_cols
            ox = col * cfg.sheet_w * MM
            oy = (cfg.tiles_rows - 1 - row) * cfg.sheet_h * MM
            nom, classe = (identities.get(num) or (None, None)) if identities else (None, None)
            draw_sheet(c, ox, oy, cfg, sheet_number=num, side=side, nom=nom, classe=classe)
        c.setDash(3, 3)
        c.setStrokeColorRGB(0.5, 0.5, 0.5)
        c.setLineWidth(0.5)
        for col in range(1, cfg.tiles_cols):
            x = col * cfg.sheet_w * MM
            c.line(x, 0, x, page_h * MM)
        for row in range(1, cfg.tiles_rows):
            y = row * cfg.sheet_h * MM
            c.line(0, y, page_w * MM, y)
        c.showPage()

    for page_start in range(0, len(numbers), n_tiles):
        page_numbers = numbers[page_start: page_start + n_tiles]
        draw_tile_page(page_numbers, side=0)
        if rv:
            draw_tile_page(page_numbers, side=1)
    c.save()


def run_in_background(widget, work, on_done):
    """Runs `work()` in a separate thread (so the window doesn't freeze)
    then calls back `on_done(error, result)` on the main Tkinter thread
    via `.after()`."""
    def runner():
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - catch everything to display it
            err = (exc, traceback.format_exc())
            widget.after(0, lambda: on_done(err, None))
        else:
            widget.after(0, lambda: on_done(None, result))
    threading.Thread(target=runner, daemon=True).start()


def offer_csv_template(parent):
    """Creates a class CSV template (columns numero,nom,classe) ready to
    be filled in in Excel/LibreOffice, then opens it. The number on each
    row must match the number printed on that student's answer sheet."""
    n = simpledialog.askinteger(APP_TITLE, tr("template_ask_count"),
                                 initialvalue=30, minvalue=1, maxvalue=255, parent=parent)
    if not n:
        return None
    path = filedialog.asksaveasfilename(
        title=tr("template_save_title"), defaultextension=".csv",
        initialfile="modele_classe.csv", filetypes=[(tr("file_csv"), "*.csv")], parent=parent)
    if not path:
        return None
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["numero", "nom", "classe"])
        for i in range(1, n + 1):
            w.writerow([i, "", ""])
    messagebox.showinfo(APP_TITLE, tr("template_created", path=path))
    os.startfile(path)
    return path


def cv2_to_photoimage(bgr_image, max_w=520, max_h=None):
    import cv2
    rgb = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(rgb)
    ratio = 1.0
    if max_w and pil_img.width > max_w:
        ratio = min(ratio, max_w / pil_img.width)
    if max_h and pil_img.height > max_h:
        ratio = min(ratio, max_h / pil_img.height)
    if ratio < 1.0:
        pil_img = pil_img.resize((max(1, int(pil_img.width * ratio)), max(1, int(pil_img.height * ratio))),
                                  Image.LANCZOS)
    return ImageTk.PhotoImage(pil_img)


GRID_CELL_SIZE = 34


def grid_header_cell(parent, text):
    """Fixed-WIDTH header cell (letter A/B/C...), so the letters stay
    aligned with the checkboxes below (a Label and a Checkbutton with the
    same 'width' don't end up the same actual width on screen)."""
    cell = tk.Frame(parent, width=GRID_CELL_SIZE, height=22)
    cell.pack_propagate(False)
    cell.pack(side="left")
    ttk.Label(cell, text=text, anchor="center").pack(expand=True)


def grid_check_cell(parent, var):
    cell = tk.Frame(parent, width=GRID_CELL_SIZE, height=24)
    cell.pack_propagate(False)
    cell.pack(side="left")
    ttk.Checkbutton(cell, variable=var).pack(expand=True)


class ScrollableFrame(ttk.Frame):
    """Frame with a vertical scrollbar, used for the answer-key grid (up
    to 64 questions) and for a copy's detail view."""

    def __init__(self, master, height=420, **kwargs):
        super().__init__(master, **kwargs)
        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0, height=height)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

    def _on_inner_configure(self, event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def _on_mousewheel(self, event):
        if str(self.canvas) in str(event.widget) or self._contains(event.widget):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _contains(self, widget):
        w = widget
        while w is not None:
            if w == self.inner or w == self.canvas:
                return True
            w = w.master if hasattr(w, "master") else None
        return False


# ---------------------------------------------------------------------
# Tab 1: answer sheet generation
# ---------------------------------------------------------------------
class GenerateTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=12)
        self.settings = app_config.load_settings()
        self.roster_mode = tk.StringVar(value="manuel")
        self.csv_path = tk.StringVar(value="")
        self.loaded_csv_roster = None  # {numero: {"nom":..., "classe":...}}
        self._build_ui()

    def _build_ui(self):
        s = self.settings

        form = ttk.LabelFrame(self, text=tr("gen_sheet_group"), padding=10)
        form.pack(fill="x", pady=(0, 10))
        form.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(form, text=tr("gen_title_label")).grid(row=row, column=0, sticky="w", pady=3)
        self.title_var = tk.StringVar(value=s.get("title", tr("gen_title_default")))
        ttk.Entry(form, textvariable=self.title_var, width=40).grid(row=row, column=1, sticky="we", padx=5)

        row += 1
        ttk.Label(form, text=tr("gen_subtitle_label")).grid(row=row, column=0, sticky="w", pady=3)
        self.subtitle_var = tk.StringVar(value=s.get("subtitle", tr("gen_subtitle_default")))
        ttk.Entry(form, textvariable=self.subtitle_var, width=40).grid(row=row, column=1, sticky="we", padx=5)

        row += 1
        ttk.Label(form, text=tr("gen_n_questions_label")).grid(row=row, column=0, sticky="w", pady=3)
        self.n_questions_var = tk.IntVar(value=s.get("n_questions", 12))
        ttk.Spinbox(form, from_=1, to=MAX_QUESTIONS, textvariable=self.n_questions_var, width=8).grid(
            row=row, column=1, sticky="w", padx=5)

        row += 1
        ttk.Label(form, text=tr("gen_n_choices_label")).grid(row=row, column=0, sticky="w", pady=3)
        self.n_choices_var = tk.IntVar(value=s.get("n_choices", 5))
        ttk.Spinbox(form, from_=3, to=6, textvariable=self.n_choices_var, width=8).grid(
            row=row, column=1, sticky="w", padx=5)

        row += 1
        self.show_classe_var = tk.BooleanVar(value=s.get("show_classe", True))
        ttk.Checkbutton(form, text=tr("gen_show_classe"), variable=self.show_classe_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=3)

        row += 1
        ttk.Label(form, text=tr("gen_sheet_size")).grid(row=row, column=0, sticky="w", pady=3)
        self._preset_labels = [tr(f"preset_{pid}") for pid in SIZE_PRESET_IDS]
        self._preset_label_to_id = dict(zip(self._preset_labels, SIZE_PRESET_IDS))
        stored_preset_id = s.get("preset_id", SIZE_PRESET_IDS[0])
        if stored_preset_id not in SIZE_PRESET_IDS:
            stored_preset_id = SIZE_PRESET_IDS[0]
        self.preset_var = tk.StringVar(value=tr(f"preset_{stored_preset_id}"))
        ttk.Combobox(form, textvariable=self.preset_var, values=self._preset_labels,
                     state="readonly", width=38).grid(row=row, column=1, sticky="we", padx=5)

        roster_frame = ttk.LabelFrame(self, text=tr("gen_students_group"), padding=10)
        roster_frame.pack(fill="x", pady=(0, 10))

        modes = [
            ("manuel", tr("gen_mode_manual")),
            ("csv", tr("gen_mode_csv")),
            ("aucun", tr("gen_mode_none")),
        ]
        for value, label in modes:
            mode_row = ttk.Frame(roster_frame)
            mode_row.pack(fill="x", anchor="w")
            ttk.Radiobutton(mode_row, text=label, variable=self.roster_mode, value=value,
                            command=self._on_roster_mode_change).pack(side="left", anchor="w")
            if value == "csv":
                ttk.Button(mode_row, text=tr("btn_create_template"), command=lambda: offer_csv_template(self)).pack(
                    side="left", padx=10)

        self.manual_frame = ttk.Frame(roster_frame)
        ttk.Label(self.manual_frame, text=tr("gen_manual_names_hint")).pack(anchor="w")
        self.names_text = tk.Text(self.manual_frame, height=8, width=45)
        self.names_text.pack(fill="x", pady=3)
        classe_row = ttk.Frame(self.manual_frame)
        classe_row.pack(fill="x")
        ttk.Label(classe_row, text=tr("gen_manual_classe_label")).pack(side="left")
        self.manual_classe_var = tk.StringVar(value=s.get("last_classe", ""))
        ttk.Entry(classe_row, textvariable=self.manual_classe_var, width=15).pack(side="left", padx=5)
        ttk.Button(classe_row, text=tr("gen_save_class_btn"), command=self._save_manual_class_to_memory).pack(
            side="left", padx=5)

        self.csv_frame = ttk.Frame(roster_frame)
        csv_row = ttk.Frame(self.csv_frame)
        csv_row.pack(fill="x", pady=3)
        ttk.Entry(csv_row, textvariable=self.csv_path, width=40).pack(side="left", fill="x", expand=True)
        ttk.Button(csv_row, text=tr("btn_browse"), command=self._browse_csv).pack(side="left", padx=5)
        csv_row2 = ttk.Frame(self.csv_frame)
        csv_row2.pack(fill="x", pady=(0, 3))
        ttk.Button(csv_row2, text=tr("gen_roster_table_btn"),
                   command=self._open_roster_manager).pack(side="left")
        ttk.Button(csv_row2, text=tr("gen_save_class_btn"),
                   command=self._save_class_to_memory).pack(side="left", padx=5)
        self.csv_info_label = ttk.Label(self.csv_frame, text="", foreground="#555555")
        self.csv_info_label.pack(anchor="w")

        self.aucun_frame = ttk.Frame(roster_frame)
        n_row = ttk.Frame(self.aucun_frame)
        n_row.pack(fill="x", pady=3)
        ttk.Label(n_row, text=tr("gen_n_sheets_label")).pack(side="left")
        self.n_sheets_var = tk.IntVar(value=s.get("n_sheets", 30))
        ttk.Spinbox(n_row, from_=1, to=255, textvariable=self.n_sheets_var, width=8).pack(side="left", padx=5)
        start_row = ttk.Frame(self.aucun_frame)
        start_row.pack(fill="x", pady=3)
        ttk.Label(start_row, text=tr("gen_start_number_label")).pack(side="left")
        self.start_number_var = tk.IntVar(value=s.get("start_number", 1))
        ttk.Spinbox(start_row, from_=1, to=255, textvariable=self.start_number_var, width=8).pack(side="left", padx=5)

        identity_frame = ttk.Frame(roster_frame)
        identity_frame.pack(fill="x", pady=(8, 0), anchor="w")
        self.print_name_var = tk.BooleanVar(value=s.get("print_name", False))
        ttk.Checkbutton(identity_frame, text=tr("gen_print_name"),
                        variable=self.print_name_var).pack(anchor="w")
        self.print_classe_var = tk.BooleanVar(value=s.get("print_classe", False))
        ttk.Checkbutton(identity_frame, text=tr("gen_print_classe"),
                        variable=self.print_classe_var).pack(anchor="w")
        ttk.Label(identity_frame, text=tr("gen_print_hint"),
                  foreground="#777777").pack(anchor="w")

        self.extra_blank_frame = ttk.Frame(identity_frame)
        self.extra_blank_enabled_var = tk.BooleanVar(value=s.get("extra_blank_enabled", False))
        ttk.Checkbutton(self.extra_blank_frame, text=tr("gen_extra_blank_generate"),
                        variable=self.extra_blank_enabled_var).pack(side="left")
        self.extra_blank_n_var = tk.IntVar(value=s.get("extra_blank_n", 3))
        ttk.Spinbox(self.extra_blank_frame, from_=1, to=10, textvariable=self.extra_blank_n_var, width=4).pack(
            side="left", padx=5)
        ttk.Label(self.extra_blank_frame, text=tr("gen_extra_blank_label")).pack(side="left")
        self.print_name_var.trace_add("write", self._update_extra_blank_visibility)
        self.print_classe_var.trace_add("write", self._update_extra_blank_visibility)
        self._update_extra_blank_visibility()

        self._on_roster_mode_change()

        out_frame = ttk.LabelFrame(self, text=tr("gen_output_group"), padding=10)
        out_frame.pack(fill="x", pady=(0, 10))
        self.output_dir_var = tk.StringVar(
            value=s.get("generate_output_dir",
                         os.path.join(app_config.default_documents_dir(), tr("gen_output_dir_default"))))
        row2 = ttk.Frame(out_frame)
        row2.pack(fill="x")
        ttk.Entry(row2, textvariable=self.output_dir_var, width=50).pack(side="left", fill="x", expand=True)
        ttk.Button(row2, text=tr("btn_browse"), command=self._browse_output_dir).pack(side="left", padx=5)

        action_row = ttk.Frame(self)
        action_row.pack(fill="x", pady=6)
        self.preview_btn = ttk.Button(action_row, text=tr("gen_btn_preview"), command=self._on_preview)
        self.preview_btn.pack(side="left")
        self.generate_btn = ttk.Button(action_row, text=tr("gen_btn_generate"), command=self._on_generate)
        self.generate_btn.pack(side="left", padx=(8, 0))
        self.progress = ttk.Progressbar(action_row, mode="indeterminate", length=200)
        self.progress.pack(side="left", padx=10)

        self.status_label = ttk.Label(self, text="", foreground="#333333", wraplength=760, justify="left")
        self.status_label.pack(fill="x", pady=(6, 0))

    def _update_extra_blank_visibility(self, *_):
        if self.print_name_var.get() or self.print_classe_var.get():
            if not self.extra_blank_frame.winfo_ismapped():
                self.extra_blank_frame.pack(fill="x", pady=(4, 0), anchor="w")
        else:
            self.extra_blank_frame.pack_forget()

    def _on_roster_mode_change(self):
        for f in (self.manual_frame, self.csv_frame, self.aucun_frame):
            f.pack_forget()
        mode = self.roster_mode.get()
        if mode == "manuel":
            self.manual_frame.pack(fill="x", pady=(6, 0))
        elif mode == "csv":
            self.csv_frame.pack(fill="x", pady=(6, 0))
        else:
            self.aucun_frame.pack(fill="x", pady=(6, 0))

    def _browse_csv(self):
        path = filedialog.askopenfilename(title=tr("csv_choose_file"),
                                           filetypes=[(tr("file_csv"), "*.csv"), (tr("file_all"), "*.*")])
        if not path:
            return
        try:
            roster, use_path, message = smart_load_roster_with_export(path)
        except (OSError, KeyError, ValueError) as exc:
            messagebox.showerror(APP_TITLE, tr("csv_read_error", detail=exc))
            return
        if message:
            messagebox.showinfo(APP_TITLE, message)
        self.csv_path.set(use_path)
        self.loaded_csv_roster = roster
        self.csv_info_label.config(text=tr("csv_loaded_with_range", n=len(roster), min=min(roster), max=max(roster)))

    def _open_roster_manager(self):
        def on_apply(roster, name):
            self.loaded_csv_roster = roster
            self.csv_path.set(tr("csv_path_from_memory", name=name) if name else tr("csv_path_manual_edit"))
            self.csv_info_label.config(text=tr("csv_loaded_with_range", n=len(roster), min=min(roster), max=max(roster)))
            self.roster_mode.set("csv")
            self._on_roster_mode_change()

        RosterManagerDialog(self, initial_roster=self.loaded_csv_roster,
                             initial_name=suggest_class_name(self.loaded_csv_roster) if self.loaded_csv_roster
                             else None,
                             on_apply=on_apply)

    def _save_class_to_memory(self):
        self._save_roster_to_memory(self.loaded_csv_roster)

    def _save_manual_class_to_memory(self):
        raw = self.names_text.get("1.0", "end").splitlines()
        names = [n.strip() for n in raw if n.strip()]
        if not names:
            messagebox.showwarning(APP_TITLE, tr("gen_names_required"))
            return
        classe_for_all = self.manual_classe_var.get().strip()
        roster = {i + 1: {"nom": name, "classe": classe_for_all} for i, name in enumerate(names)}
        self._save_roster_to_memory(roster)

    def _save_roster_to_memory(self, roster):
        if not roster:
            messagebox.showwarning(APP_TITLE, tr("gen_roster_load_warn"))
            return
        suggested = suggest_class_name(roster)
        name = simpledialog.askstring(APP_TITLE, tr("gen_ask_class_name"),
                                       initialvalue=suggested, parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in class_archive.list_classes():
            if not messagebox.askyesno(APP_TITLE, tr("gen_class_exists_replace", name=name)):
                return
        class_archive.save_roster(name, roster)
        messagebox.showinfo(APP_TITLE, tr("gen_class_saved", name=name, n=len(roster)))

    def _browse_output_dir(self):
        d = filedialog.askdirectory(title=tr("gen_choose_output_dir"))
        if d:
            self.output_dir_var.set(d)

    def _build_config(self):
        preset_id = self._preset_label_to_id.get(self.preset_var.get(), SIZE_PRESET_IDS[0])
        preset = SIZE_PRESET_PARAMS[preset_id]
        return SheetConfig(
            title=self.title_var.get().strip() or tr("gen_title_default"),
            subtitle=self.subtitle_var.get().strip(),
            n_questions=int(self.n_questions_var.get()),
            n_choices=int(self.n_choices_var.get()),
            show_classe=bool(self.show_classe_var.get()),
            scale=preset["scale"],
            tiles_rows=preset["tiles_rows"],
            tiles_cols=preset["tiles_cols"],
            lang=translations.get_language(),
        )

    def _save_settings(self):
        s = self.settings
        s.update(dict(
            title=self.title_var.get(), subtitle=self.subtitle_var.get(),
            n_questions=int(self.n_questions_var.get()), n_choices=int(self.n_choices_var.get()),
            show_classe=bool(self.show_classe_var.get()),
            preset_id=self._preset_label_to_id.get(self.preset_var.get(), SIZE_PRESET_IDS[0]),
            n_sheets=int(self.n_sheets_var.get()), start_number=int(self.start_number_var.get()),
            last_classe=self.manual_classe_var.get(), generate_output_dir=self.output_dir_var.get(),
            print_name=bool(self.print_name_var.get()), print_classe=bool(self.print_classe_var.get()),
            extra_blank_enabled=bool(self.extra_blank_enabled_var.get()),
            extra_blank_n=int(self.extra_blank_n_var.get()),
        ))
        app_config.save_settings(s)

    def _on_preview(self):
        try:
            cfg = self._build_config()
            cfg.validate()
        except (tk.TclError, ValueError) as exc:
            messagebox.showerror(APP_TITLE, tr("gen_invalid_config", detail=exc))
            return

        n_tiles = cfg.tiles_rows * cfg.tiles_cols
        fd, preview_path = tempfile.mkstemp(prefix="qcm_apercu_", suffix=".pdf")
        os.close(fd)
        try:
            build(preview_path, cfg, numbers=list(range(1, n_tiles + 1)))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, tr("gen_preview_failed", detail=exc))
            return
        os.startfile(preview_path)

    def _on_generate(self):
        try:
            cfg = self._build_config()
        except (tk.TclError, ValueError) as exc:
            messagebox.showerror(APP_TITLE, tr("gen_invalid_config_short", detail=exc))
            return

        mode = self.roster_mode.get()
        names, classe_for_all, numbers_from_csv, roster_rows = None, "", None, None
        if mode == "manuel":
            raw = self.names_text.get("1.0", "end").splitlines()
            names = [n.strip() for n in raw if n.strip()]
            if not names:
                messagebox.showwarning(APP_TITLE, tr("gen_names_required"))
                return
            classe_for_all = self.manual_classe_var.get().strip()
        elif mode == "csv":
            if not self.loaded_csv_roster:
                messagebox.showwarning(APP_TITLE, tr("gen_csv_required"))
                return
            roster_rows = self.loaded_csv_roster
            numbers_from_csv = sorted(roster_rows.keys())

        output_dir = self.output_dir_var.get().strip()
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, tr("gen_output_dir_error", detail=exc))
            return

        self.generate_btn.config(state="disabled")
        self.progress.start(12)
        self.status_label.config(text=tr("gen_generating"))

        def work():
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            pdf_path = os.path.join(output_dir, f"feuilles_{timestamp}.pdf")
            csv_path = None
            print_name = bool(self.print_name_var.get())
            print_classe = bool(self.print_classe_var.get())
            n_extra = 0
            if (print_name or print_classe) and self.extra_blank_enabled_var.get():
                n_extra = int(self.extra_blank_n_var.get())
            if mode == "manuel":
                start_number = int(self.start_number_var.get())
                identities = None
                if print_name or print_classe:
                    preview_numbers = range(start_number, start_number + len(names))
                    identities = {
                        num: (name if print_name else None,
                              classe_for_all if (print_classe and classe_for_all) else None)
                        for num, name in zip(preview_numbers, names)
                    }
                generated_numbers = build_batch(pdf_path, cfg, start_number=start_number,
                                                 n_sheets=len(names) + n_extra, identities=identities)
                csv_path = os.path.join(output_dir, f"classe_{timestamp}.csv")
                with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f, delimiter=";")
                    w.writerow(["numero", "nom", "classe"])
                    for num, name in zip(generated_numbers, names):
                        w.writerow([num, name, classe_for_all])
            elif mode == "csv":
                identities = None
                if print_name or print_classe:
                    identities = {
                        num: (roster_rows[num]["nom"] if print_name else None,
                              roster_rows[num].get("classe") if (print_classe and roster_rows[num].get("classe"))
                              else None)
                        for num in numbers_from_csv
                    }
                extra_numbers = list(range(max(numbers_from_csv) + 1, max(numbers_from_csv) + 1 + n_extra))
                generated_numbers = numbers_from_csv + extra_numbers
                build_for_numbers(pdf_path, cfg, generated_numbers, identities=identities)
            else:
                start_number = int(self.start_number_var.get())
                n_sheets = int(self.n_sheets_var.get())
                generated_numbers = build_batch(pdf_path, cfg, start_number=start_number, n_sheets=n_sheets)
            return pdf_path, csv_path, generated_numbers, cfg, n_extra

        def on_done(err, result):
            self.progress.stop()
            self.generate_btn.config(state="normal")
            if err:
                exc, tb = err
                messagebox.showerror(APP_TITLE, tr("gen_failed", detail=exc))
                self.status_label.config(text="")
                return
            pdf_path, csv_path, generated_numbers, used_cfg, n_extra = result
            self._save_settings()
            rv = tr("gen_rv_suffix") if needs_recto_verso(used_cfg) else ""
            msg = tr("gen_success_summary", n=len(generated_numbers), start=min(generated_numbers),
                      end=max(generated_numbers), rv=rv, path=pdf_path)
            if n_extra:
                extra_start = max(generated_numbers) - n_extra + 1
                msg += tr("gen_extra_blank_summary", n=n_extra, start=extra_start, end=max(generated_numbers))
            if csv_path:
                msg += tr("gen_csv_class_line", path=csv_path)
            self.status_label.config(text=msg)
            if messagebox.askyesno(APP_TITLE, msg + tr("gen_open_folder_now")):
                os.startfile(output_dir)

        run_in_background(self, work, on_done)


# ---------------------------------------------------------------------
# Answer key entry window (optional)
# ---------------------------------------------------------------------
class AnswerKeyDialog(tk.Toplevel):
    """Lets the teacher define a corrigé: correct answer(s) AND a
    per-question point value (barème), plus two grading options that
    apply to the whole corrigé (negative_points, partial_credit -- see
    scoring.py for exactly how they affect the grade). Corrigés can be
    saved/loaded/renamed/deleted by name (answer_key_store.py) so the
    SAME corrigé can be reused across several classes/batches of copies
    of the same test, rather than re-entered by hand each time."""

    def __init__(self, master, n_questions, n_choices, existing_key=None, existing_name=None):
        super().__init__(master)
        self.title(tr("ak_title"))
        self.geometry("640x720")
        self.result = None
        self.validated = False
        self.result_name = None
        self._initial_key = (existing_key or {}).get("questions", {})
        self.letters = []
        self.vars = {}
        self.points_vars = {}

        ttk.Label(self, text=tr("ak_hint"),
                  justify="left", wraplength=600).pack(fill="x", padx=10, pady=8)

        store_frame = ttk.LabelFrame(self, text=tr("ak_store_group"), padding=10)
        store_frame.pack(fill="x", padx=10, pady=(0, 8))
        row1 = ttk.Frame(store_frame)
        row1.pack(fill="x")
        ttk.Label(row1, text=tr("ak_store_label")).pack(side="left")
        self.store_combo = ttk.Combobox(row1, state="readonly", width=22)
        self.store_combo.pack(side="left", padx=5)
        ttk.Button(row1, text=tr("btn_load"), command=self._on_load_from_store).pack(side="left", padx=2)
        ttk.Button(row1, text=tr("btn_rename"), command=self._on_rename_in_store).pack(side="left", padx=2)
        ttk.Button(row1, text=tr("btn_delete"), command=self._on_delete_from_store).pack(side="left", padx=2)
        self._refresh_store_combo(select=existing_name)
        save_row = ttk.Frame(store_frame)
        save_row.pack(fill="x", pady=(6, 0))
        ttk.Label(save_row, text=tr("ak_save_as_label")).pack(side="left")
        self.save_name_var = tk.StringVar(value=existing_name or "")
        ttk.Entry(save_row, textvariable=self.save_name_var, width=22).pack(side="left", padx=5)
        ttk.Button(save_row, text=tr("rmd_save_to_memory"), command=self._on_save_to_store).pack(
            side="left", padx=3)

        top_row = ttk.Frame(self)
        top_row.pack(fill="x", padx=10)
        ttk.Label(top_row, text=tr("ak_n_questions_label")).pack(side="left")
        self.n_q_var = tk.IntVar(value=n_questions)
        ttk.Spinbox(top_row, from_=1, to=MAX_QUESTIONS, textvariable=self.n_q_var, width=6,
                    command=self._rebuild_grid).pack(side="left", padx=5)
        ttk.Label(top_row, text=tr("ak_n_choices_label")).pack(side="left", padx=(15, 0))
        self.n_choices_var = tk.IntVar(value=n_choices)
        ttk.Spinbox(top_row, from_=3, to=6, textvariable=self.n_choices_var, width=6,
                    command=self._rebuild_grid).pack(side="left", padx=5)

        options_row = ttk.Frame(self)
        options_row.pack(fill="x", padx=10, pady=(8, 0))
        self.negative_var = tk.BooleanVar(value=(existing_key or {}).get("negative_points", False))
        ttk.Checkbutton(options_row, text=tr("ak_negative_points"), variable=self.negative_var).pack(
            anchor="w")
        self.partial_var = tk.BooleanVar(value=(existing_key or {}).get("partial_credit", False))
        ttk.Checkbutton(options_row, text=tr("ak_partial_credit"), variable=self.partial_var).pack(
            anchor="w")

        self.scroll = ScrollableFrame(self, height=340)
        self.scroll.pack(fill="both", expand=True, padx=10, pady=8)

        self._rebuild_grid()

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=10, pady=8)
        ttk.Button(btn_row, text=tr("ak_clear_all"), command=self._clear_all).pack(side="left")
        ttk.Button(btn_row, text=tr("btn_cancel"), command=self.destroy).pack(side="right")
        ttk.Button(btn_row, text=tr("btn_validate"), command=self._on_validate).pack(side="right", padx=5)

        self.transient(master)
        self.grab_set()

    def _clear_all(self):
        for v in self.vars.values():
            v.set(False)

    def _current_key(self):
        """Current grid content as {q: {"correct": [...], "points":
        "text typed"}} -- used only to preserve entries across a
        _rebuild_grid (n_questions/n_choices change), so points stay as
        raw text here (not yet validated as a number)."""
        key = {}
        for q in sorted({q for (q, _letter) in self.vars.keys()}):
            letters = [letter for letter in self.letters if self.vars[(q, letter)].get()]
            points_text = self.points_vars[q].get() if q in self.points_vars else "1"
            key[q] = {"correct": letters, "points": points_text}
        return key

    def _rebuild_grid(self):
        # Preserves already-checked boxes/points when the number of
        # questions or choices changes (otherwise changing one clears
        # the other).
        prev_key = self._current_key() if self.vars else {
            q: {"correct": info.get("correct", []), "points": _fmt_points(info.get("points", 1.0))}
            for q, info in self._initial_key.items()
        }
        for child in self.scroll.inner.winfo_children():
            child.destroy()
        self.vars = {}
        self.points_vars = {}
        n_q = int(self.n_q_var.get())
        n_c = int(self.n_choices_var.get())
        self.letters = ["A", "B", "C", "D", "E", "F"][:n_c]
        header = ttk.Frame(self.scroll.inner)
        header.pack(fill="x")
        ttk.Label(header, text=tr("ak_question_col"), width=10).pack(side="left")
        ttk.Label(header, text=tr("ak_points_col"), width=7).pack(side="left")
        for letter in self.letters:
            grid_header_cell(header, letter)
        for q in range(1, n_q + 1):
            prev = prev_key.get(q, {})
            row = ttk.Frame(self.scroll.inner)
            row.pack(fill="x")
            ttk.Label(row, text=f"Q{q}", width=10).pack(side="left")
            points_var = tk.StringVar(value=prev.get("points", "1"))
            self.points_vars[q] = points_var
            ttk.Entry(row, textvariable=points_var, width=6).pack(side="left", padx=(2, 8))
            for letter in self.letters:
                v = tk.BooleanVar(value=letter in prev.get("correct", []))
                self.vars[(q, letter)] = v
                grid_check_cell(row, v)

    def _on_validate(self):
        n_q = int(self.n_q_var.get())
        questions = {}
        errors = []
        for q in range(1, n_q + 1):
            letters = [letter for letter in self.letters if self.vars[(q, letter)].get()]
            if not letters:
                continue
            points_text = self.points_vars[q].get().strip().replace(",", ".")
            try:
                points = float(points_text) if points_text else 1.0
            except ValueError:
                points = None
            if points is None or points <= 0:
                errors.append(tr("ak_invalid_points_for", q=q))
                continue
            questions[q] = {"correct": letters, "points": points}
        if errors:
            messagebox.showerror(APP_TITLE, tr("rmd_fix_first", errors="\n".join(errors)))
            return
        if not questions:
            if not messagebox.askyesno(APP_TITLE, tr("ak_no_key_confirm")):
                return
            self.result = None
        else:
            self.result = {"questions": questions, "negative_points": self.negative_var.get(),
                            "partial_credit": self.partial_var.get()}
        self.result_name = self.save_name_var.get().strip() or None
        self.validated = True
        self.destroy()

    # -- Corrigés saved to memory -------------------------------------
    def _refresh_store_combo(self, select=None):
        names = answer_key_store.list_keys()
        self.store_combo.configure(values=names)
        if select and select in names:
            self.store_combo.set(select)

    def _apply_loaded_key(self, key):
        self._initial_key = key.get("questions", {})
        self.n_q_var.set(max(self._initial_key.keys(), default=int(self.n_q_var.get())))
        max_choice_count = max((len(info.get("correct", [])) for info in self._initial_key.values()),
                                default=int(self.n_choices_var.get()))
        # n_choices isn't derivable from the key alone (a question can
        # have fewer correct letters than there are choices on the
        # sheet) -- only bump it up if a saved question needs more
        # choices than currently set, never shrink it.
        self.n_choices_var.set(max(int(self.n_choices_var.get()), min(max_choice_count, 6)))
        self.negative_var.set(key.get("negative_points", False))
        self.partial_var.set(key.get("partial_credit", False))
        self.vars = {}
        self._rebuild_grid()

    def _on_load_from_store(self):
        name = self.store_combo.get()
        if not name:
            messagebox.showinfo(APP_TITLE, tr("rmd_choose_class_first"))
            return
        key = answer_key_store.load_key(name)
        if key is None:
            return
        self._apply_loaded_key(key)
        self.save_name_var.set(name)

    def _on_rename_in_store(self):
        name = self.store_combo.get()
        if not name:
            messagebox.showinfo(APP_TITLE, tr("rmd_choose_class_first"))
            return
        new_name = simpledialog.askstring(APP_TITLE, tr("ak_new_name_for", name=name), initialvalue=name,
                                           parent=self)
        if not new_name or new_name.strip() == name:
            return
        new_name = new_name.strip()
        if not answer_key_store.rename_key(name, new_name):
            messagebox.showerror(APP_TITLE, tr("ak_rename_target_exists", name=new_name))
            return
        self._refresh_store_combo(select=new_name)
        if self.save_name_var.get() == name:
            self.save_name_var.set(new_name)

    def _on_delete_from_store(self):
        name = self.store_combo.get()
        if not name:
            messagebox.showinfo(APP_TITLE, tr("rmd_choose_class_first"))
            return
        if messagebox.askyesno(APP_TITLE, tr("ak_confirm_delete", name=name)):
            answer_key_store.delete_key(name)
            self._refresh_store_combo()

    def _on_save_to_store(self):
        n_q = int(self.n_q_var.get())
        questions = {}
        errors = []
        for q in range(1, n_q + 1):
            letters = [letter for letter in self.letters if self.vars[(q, letter)].get()]
            if not letters:
                continue
            points_text = self.points_vars[q].get().strip().replace(",", ".")
            try:
                points = float(points_text) if points_text else 1.0
            except ValueError:
                points = None
            if points is None or points <= 0:
                errors.append(tr("ak_invalid_points_for", q=q))
                continue
            questions[q] = {"correct": letters, "points": points}
        if errors:
            messagebox.showerror(APP_TITLE, tr("rmd_fix_first", errors="\n".join(errors)))
            return
        if not questions:
            messagebox.showwarning(APP_TITLE, tr("ak_no_key_to_save"))
            return
        name = self.save_name_var.get().strip()
        if not name:
            messagebox.showwarning(APP_TITLE, tr("ak_name_required"))
            return
        if name in answer_key_store.list_keys():
            if not messagebox.askyesno(APP_TITLE, tr("gen_class_exists_replace", name=name)):
                return
        answer_key_store.save_key(name, {"questions": questions, "negative_points": self.negative_var.get(),
                                          "partial_credit": self.partial_var.get()})
        self._refresh_store_combo(select=name)
        messagebox.showinfo(APP_TITLE, tr("ak_key_saved", name=name, n=len(questions)))


# ---------------------------------------------------------------------
# Copy detail window (manual matching, editing detected answers,
# viewing the cropped Name/Class area).
# ---------------------------------------------------------------------
class DetailDialog(tk.Toplevel):
    def __init__(self, master, entry, roster, on_save):
        super().__init__(master)
        self.entry = entry
        self.roster = roster or {}
        self.on_save = on_save
        num = entry.get("sheet_number")
        num_label = tr("dd_number_label", num=num) if num is not None else tr("dd_number_unset")
        self.title(tr("dd_title", num=num_label))

        status = entry.get("status")
        header = ttk.Frame(self, padding=10)
        header.pack(fill="x")
        ttk.Label(header, text=tr("dd_header", num=num_label, status=status_label(status)),
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        if status == "face_manquante":
            faces = ", ".join(tr("dd_side_recto") if s == 0 else tr("dd_side_verso")
                               for s in entry.get("missing_sides", []))
            ttk.Label(header, text=tr("dd_missing_sides", faces=faces),
                      wraplength=580, foreground="#a05a00", justify="left").pack(anchor="w", pady=4)
            expected = self.roster.get(num)
            if expected:
                ttk.Label(header, text=tr("dd_expected_student", name=expected['nom'])).pack(anchor="w")
            ttk.Button(self, text=tr("btn_close"), command=self.destroy).pack(pady=10)
            self.geometry("640x680")
            self.transient(master)
            self.grab_set()
            return

        img_frame = ttk.Frame(self, padding=(10, 0))
        img_frame.pack(fill="x")
        self._photo = None
        crop_path = entry.get("identity_crop_path")
        if crop_path and os.path.exists(crop_path):
            import cv2
            crop = cv2.imread(crop_path)
            if crop is not None:
                self._photo = cv2_to_photoimage(crop)
                ttk.Label(img_frame, image=self._photo).pack(anchor="w", pady=4)
        else:
            ttk.Label(img_frame, text=tr("dd_no_crop"),
                      foreground="#777777").pack(anchor="w")
        sources = entry.get("sources") or []
        if sources:
            ttk.Button(img_frame, text=tr("dd_open_original"),
                       command=lambda: os.startfile(sources[0])).pack(anchor="w", pady=(0, 6))

        identity_frame = ttk.Frame(self, padding=10)
        identity_frame.pack(fill="x")
        ttk.Label(identity_frame, text=tr("dd_student_label")).grid(row=0, column=0, sticky="w")
        self.nom_var = tk.StringVar(value=entry.get("eleve", ""))
        ttk.Entry(identity_frame, textvariable=self.nom_var, width=30).grid(row=0, column=1, sticky="w", padx=5)
        ttk.Label(identity_frame, text=tr("dd_class_label")).grid(row=0, column=2, sticky="w", padx=(15, 0))
        self.classe_var = tk.StringVar(value=entry.get("classe", ""))
        ttk.Entry(identity_frame, textvariable=self.classe_var, width=12).grid(row=0, column=3, sticky="w", padx=5)

        ttk.Label(self, text=tr("dd_answers_hint"),
                  padding=(10, 4)).pack(anchor="w")
        self.scroll = ScrollableFrame(self, height=300)
        self.scroll.pack(fill="both", expand=True, padx=10, pady=4)

        self.answer_vars = {}
        questions = entry.get("questions", {})
        for q in sorted(questions.keys()):
            qres = questions[q]
            row = ttk.Frame(self.scroll.inner)
            row.pack(fill="x", pady=1)
            flagged = bool(qres.get("flagged"))
            label_fg = "#a05a00" if flagged else "#000000"
            ttk.Label(row, text=f"Q{q}", width=6, foreground=label_fg).pack(side="left")
            current = ",".join(qres.get("answers", []))
            v = tk.StringVar(value=current)
            self.answer_vars[q] = v
            ttk.Entry(row, textvariable=v, width=10).pack(side="left", padx=5)
            if flagged:
                ttk.Label(row, text=tr("dd_to_check", letters=", ".join(qres['flagged'])),
                          foreground="#a05a00").pack(side="left", padx=5)

        btn_row = ttk.Frame(self, padding=10)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text=tr("btn_cancel"), command=self.destroy).pack(side="right")
        ttk.Button(btn_row, text=tr("btn_save"), command=self._on_save_click).pack(side="right", padx=5)

        self.geometry("640x680")
        self.transient(master)
        self.grab_set()

    def _on_save_click(self):
        nom = self.nom_var.get().strip()
        classe = self.classe_var.get().strip()
        questions = self.entry.get("questions", {})
        for q, v in self.answer_vars.items():
            raw = v.get().strip().upper()
            letters = [p.strip() for p in raw.replace(" ", ",").split(",") if p.strip()]
            questions[q]["answers"] = letters
            questions[q]["flagged"] = []
        if nom:
            self.entry["eleve"] = nom
            self.entry["classe"] = classe
            self.entry["status"] = "ok"
        self.on_save(self.entry)
        self.destroy()


# ---------------------------------------------------------------------
# Manual entry window for a photo that failed to read (damaged/torn
# markers, badly framed sheet...). Automatic detection is impossible
# without the markers, so the teacher reads the copy themselves (with a
# simple display rotation to right it) and enters number, student and
# answers by hand.
# ---------------------------------------------------------------------
class ManualEntryDialog(tk.Toplevel):
    def __init__(self, master, entry, roster, default_n_questions, default_n_choices, on_save):
        super().__init__(master)
        self.entry = entry
        self.roster = roster or {}
        self.on_save = on_save
        self.rotation = 0
        self.vars = {}
        self._photo = None
        self.title(tr("med_title"))

        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        win_w = min(1180, int(screen_w * 0.92))
        win_h = min(840, int(screen_h * 0.88))
        self.geometry(f"{win_w}x{win_h}")
        self._img_max_w = int(win_w * 0.45)
        self._img_max_h = win_h - 190

        error_text = entry.get('erreur') or tr("med_unknown_reason")
        ttk.Label(self, text=tr("med_hint", error=error_text),
                  foreground="#a03030", wraplength=win_w - 20, justify="left", padding=10).pack(fill="x")

        # Buttons pinned to the bottom BEFORE the main content: they stay
        # visible even if the content above is very tall.
        btn_row = ttk.Frame(self, padding=10)
        btn_row.pack(side="bottom", fill="x")
        ttk.Button(btn_row, text=tr("btn_cancel"), command=self.destroy).pack(side="right")
        ttk.Button(btn_row, text=tr("med_btn_save"), command=self._on_save_click).pack(
            side="right", padx=5)

        main_frame = ttk.Frame(self)
        main_frame.pack(fill="both", expand=True, padx=10)

        # --- Left column: the photo ---
        left = ttk.Frame(main_frame)
        left.pack(side="left", fill="y", anchor="n")
        self._img_label = ttk.Label(left)
        self._img_label.pack(anchor="n")
        self._raw_img = None
        self.source = entry.get("source")
        if self.source and os.path.exists(self.source):
            import cv2
            self._raw_img = cv2.imread(self.source)
        self._refresh_image()

        btn_row_img = ttk.Frame(left)
        btn_row_img.pack(anchor="w", pady=4)
        ttk.Button(btn_row_img, text=tr("med_rotate"), command=self._rotate).pack(side="left")
        if self.source:
            ttk.Button(btn_row_img, text=tr("dd_open_original"),
                       command=lambda: os.startfile(self.source)).pack(side="left", padx=5)

        # --- Right column: identity + answers ---
        right = ttk.Frame(main_frame)
        right.pack(side="left", fill="both", expand=True, padx=(15, 0))

        identity_frame = ttk.Frame(right)
        identity_frame.pack(fill="x", pady=(0, 4))
        ttk.Label(identity_frame, text=tr("med_number_label")).grid(
            row=0, column=0, sticky="w", pady=2)
        self.numero_var = tk.StringVar(value="")
        ttk.Entry(identity_frame, textvariable=self.numero_var, width=8).grid(row=0, column=1, sticky="w", padx=5)
        ttk.Button(identity_frame, text=tr("med_autofill_btn"),
                   command=self._on_autofill_name_click).grid(row=0, column=2, sticky="w", padx=5)
        ttk.Label(identity_frame, text=tr("dd_student_label")).grid(row=1, column=0, sticky="w", pady=2)
        self.nom_var = tk.StringVar(value="")
        ttk.Entry(identity_frame, textvariable=self.nom_var, width=28).grid(row=1, column=1, sticky="w", padx=5)
        ttk.Label(identity_frame, text=tr("dd_class_label")).grid(row=2, column=0, sticky="w", pady=2)
        self.classe_var = tk.StringVar(value="")
        ttk.Entry(identity_frame, textvariable=self.classe_var, width=12).grid(row=2, column=1, sticky="w", padx=5)

        grid_row = ttk.Frame(right)
        grid_row.pack(fill="x", pady=(4, 4))
        ttk.Label(grid_row, text=tr("med_n_questions_label")).pack(side="left")
        self.n_q_var = tk.IntVar(value=default_n_questions)
        ttk.Spinbox(grid_row, from_=1, to=MAX_QUESTIONS, textvariable=self.n_q_var, width=6,
                    command=self._rebuild_grid).pack(side="left", padx=5)
        ttk.Label(grid_row, text=tr("med_n_choices_label")).pack(side="left", padx=(15, 0))
        self.n_c_var = tk.IntVar(value=default_n_choices)
        ttk.Spinbox(grid_row, from_=3, to=6, textvariable=self.n_c_var, width=6,
                    command=self._rebuild_grid).pack(side="left", padx=5)

        ttk.Label(right, text=tr("med_answers_hint")).pack(
            anchor="w", pady=(4, 2))
        self.scroll = ScrollableFrame(right, height=max(200, win_h - 340))
        self.scroll.pack(fill="both", expand=True)
        self._rebuild_grid()

        self.transient(master)
        self.grab_set()

    def _on_autofill_name_click(self):
        raw = self.numero_var.get().strip()
        if not raw.isdigit():
            messagebox.showwarning(APP_TITLE, tr("med_number_required_first"))
            return
        num = int(raw)
        student = self.roster.get(num)
        if not student:
            messagebox.showinfo(APP_TITLE, tr("med_no_student_for_number", num=num))
            return
        self.nom_var.set(student["nom"])
        self.classe_var.set(student.get("classe", ""))

    def _refresh_image(self):
        if self._raw_img is None:
            self._img_label.configure(text=tr("med_photo_missing"), image="")
            return
        import cv2
        img = self._raw_img
        for _ in range(self.rotation // 90):
            img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
        self._photo = cv2_to_photoimage(img, max_w=self._img_max_w, max_h=self._img_max_h)
        self._img_label.configure(image=self._photo, text="")

    def _rotate(self):
        self.rotation = (self.rotation + 90) % 360
        self._refresh_image()

    def _current_answers(self):
        n_q = int(self.n_q_var.get())
        n_c = int(self.n_c_var.get())
        letters = ["A", "B", "C", "D", "E", "F"][:n_c]
        result = {}
        for q in range(1, n_q + 1):
            result[q] = [letter for letter in letters if self.vars.get((q, letter), tk.BooleanVar(value=False)).get()]
        return result

    def _rebuild_grid(self):
        prev = self._current_answers() if self.vars else {}
        for child in self.scroll.inner.winfo_children():
            child.destroy()
        self.vars = {}
        n_q = int(self.n_q_var.get())
        n_c = int(self.n_c_var.get())
        letters = ["A", "B", "C", "D", "E", "F"][:n_c]
        header = ttk.Frame(self.scroll.inner)
        header.pack(fill="x")
        ttk.Label(header, text=tr("ak_question_col"), width=10).pack(side="left")
        for letter in letters:
            grid_header_cell(header, letter)
        for q in range(1, n_q + 1):
            row = ttk.Frame(self.scroll.inner)
            row.pack(fill="x")
            ttk.Label(row, text=f"Q{q}", width=10).pack(side="left")
            for letter in letters:
                v = tk.BooleanVar(value=letter in prev.get(q, []))
                self.vars[(q, letter)] = v
                grid_check_cell(row, v)

    def _on_save_click(self):
        numero = None
        raw_numero = self.numero_var.get().strip()
        if raw_numero:
            try:
                numero = int(raw_numero)
            except ValueError:
                messagebox.showwarning(APP_TITLE, tr("med_invalid_number"))
                return
        if numero is None:
            if not messagebox.askyesno(APP_TITLE, tr("med_no_number_confirm")):
                return
        nom = self.nom_var.get().strip()
        classe = self.classe_var.get().strip()
        answers = self._current_answers()
        questions = {q: {"answers": letters, "flagged": [], "confidence": {}} for q, letters in answers.items()}
        new_entry = {
            "sheet_number": numero,
            "sources": [self.source] if self.source else [],
            "questions": questions,
            "status": "ok" if nom else "numero_inconnu",
        }
        if nom:
            new_entry["eleve"] = nom
            new_entry["classe"] = classe
        self.on_save(new_entry)
        self.destroy()


class RosterTableEditor(ttk.Frame):
    """Reusable editable numero/nom/classe grid: add/remove rows, load a
    roster dict into it, extract it back out with validation. Mirrors
    RosterManagerDialog's table (kept separate rather than shared, so
    editing this one can't regress that already-working dialog); used by
    DataTab to let the teacher retouch/complete a class's saved roster."""

    def __init__(self, master, height=260):
        super().__init__(master)
        self.rows = []
        header = ttk.Frame(self)
        header.pack(fill="x")
        ttk.Label(header, text=tr("col_number"), width=8).pack(side="left", padx=2)
        ttk.Label(header, text=tr("col_name"), width=28).pack(side="left", padx=2)
        ttk.Label(header, text=tr("col_class"), width=12).pack(side="left", padx=2)
        self.scroll = ScrollableFrame(self, height=height)
        self.scroll.pack(fill="both", expand=True, pady=(4, 0))
        ttk.Button(self, text=tr("btn_add_row"), command=lambda: self.add_row()).pack(anchor="w", pady=6)

    def add_row(self, numero="", nom="", classe=""):
        row_frame = ttk.Frame(self.scroll.inner)
        row_frame.pack(fill="x", pady=1)
        row_data = {"frame": row_frame}
        row_data["numero"] = tk.StringVar(value=str(numero) if numero != "" else "")
        row_data["nom"] = tk.StringVar(value=nom)
        row_data["classe"] = tk.StringVar(value=classe)
        ttk.Entry(row_frame, textvariable=row_data["numero"], width=8).pack(side="left", padx=2)
        ttk.Entry(row_frame, textvariable=row_data["nom"], width=28).pack(side="left", padx=2)
        ttk.Entry(row_frame, textvariable=row_data["classe"], width=12).pack(side="left", padx=2)
        ttk.Button(row_frame, text="✕", width=3, command=lambda: self.delete_row(row_data)).pack(
            side="left", padx=2)
        self.rows.append(row_data)
        return row_data

    def delete_row(self, row_data):
        row_data["frame"].destroy()
        self.rows.remove(row_data)

    def clear_rows(self):
        for row in list(self.rows):
            self.delete_row(row)

    def load_roster(self, roster):
        self.clear_rows()
        for num in sorted(roster.keys()):
            info = roster[num]
            self.add_row(num, info.get("nom", ""), info.get("classe", ""))

    def extract_roster(self):
        roster, errors, seen = {}, [], set()
        for row in self.rows:
            nom = row["nom"].get().strip()
            numero_str = row["numero"].get().strip()
            classe = row["classe"].get().strip()
            if not nom and not numero_str:
                continue
            if not numero_str.isdigit():
                errors.append(tr("rmd_invalid_number_for", name=nom or tr("rmd_no_name")))
                continue
            num = int(numero_str)
            if num in seen:
                errors.append(tr("rmd_number_used_twice", num=num))
                continue
            if not nom:
                errors.append(tr("rmd_name_missing_for", num=num))
                continue
            seen.add(num)
            roster[num] = {"nom": nom, "classe": classe}
        return roster, errors


# ---------------------------------------------------------------------
# Single window to view/edit a numero/nom/classe table, and manage the
# classes saved to memory -- backed by class_archive.py, the same
# per-class archive used by DataTab, so a roster saved here is the same
# one shown/editable there: load, save, rename, delete. Used from both
# tabs.
# ---------------------------------------------------------------------
class RosterManagerDialog(tk.Toplevel):
    def __init__(self, master, initial_roster=None, initial_name=None, on_apply=None):
        super().__init__(master)
        self.on_apply = on_apply
        self.rows = []
        self.title(tr("rmd_title"))
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        self.geometry(f"{min(700, int(screen_w * 0.7))}x{min(800, int(screen_h * 0.85))}")

        store_frame = ttk.LabelFrame(self, text=tr("rmd_store_group"), padding=10)
        store_frame.pack(fill="x", padx=10, pady=(10, 6))
        row1 = ttk.Frame(store_frame)
        row1.pack(fill="x")
        ttk.Label(row1, text=tr("rmd_class_label")).pack(side="left")
        self.store_combo = ttk.Combobox(row1, state="readonly", width=25)
        self.store_combo.pack(side="left", padx=5)
        ttk.Button(row1, text=tr("btn_load"), command=self._on_load_from_store).pack(side="left", padx=3)
        ttk.Button(row1, text=tr("btn_rename"), command=self._on_rename_in_store).pack(side="left", padx=3)
        ttk.Button(row1, text=tr("btn_delete"), command=self._on_delete_from_store).pack(side="left", padx=3)
        self._refresh_store_combo(select=initial_name)

        save_row = ttk.Frame(store_frame)
        save_row.pack(fill="x", pady=(8, 0))
        ttk.Label(save_row, text=tr("rmd_save_as_label")).pack(side="left")
        self.save_name_var = tk.StringVar(value=initial_name or "")
        ttk.Entry(save_row, textvariable=self.save_name_var, width=20).pack(side="left", padx=5)
        ttk.Button(save_row, text=tr("rmd_save_to_memory"), command=self._on_save_to_store).pack(side="left", padx=3)

        table_frame = ttk.LabelFrame(self, text=tr("rmd_table_group"), padding=10)
        table_frame.pack(fill="both", expand=True, padx=10, pady=6)
        header = ttk.Frame(table_frame)
        header.pack(fill="x")
        ttk.Label(header, text=tr("col_number"), width=8).pack(side="left", padx=2)
        ttk.Label(header, text=tr("col_name"), width=28).pack(side="left", padx=2)
        ttk.Label(header, text=tr("col_class"), width=12).pack(side="left", padx=2)
        self.scroll = ScrollableFrame(table_frame, height=380)
        self.scroll.pack(fill="both", expand=True, pady=(4, 0))
        ttk.Button(table_frame, text=tr("btn_add_row"), command=lambda: self._add_row()).pack(
            anchor="w", pady=6)

        if initial_roster:
            self._load_roster_into_table(initial_roster)

        action_row = ttk.Frame(self, padding=10)
        action_row.pack(fill="x")
        ttk.Button(action_row, text=tr("btn_cancel"), command=self.destroy).pack(side="right")
        if on_apply:
            ttk.Button(action_row, text=tr("rmd_use_table"), command=self._on_use_click).pack(
                side="right", padx=5)

        self.transient(master)
        self.grab_set()

    # -- Table rows -------------------------------------------------
    def _add_row(self, numero="", nom="", classe=""):
        row_frame = ttk.Frame(self.scroll.inner)
        row_frame.pack(fill="x", pady=1)
        row_data = {"frame": row_frame}
        row_data["numero"] = tk.StringVar(value=str(numero) if numero != "" else "")
        row_data["nom"] = tk.StringVar(value=nom)
        row_data["classe"] = tk.StringVar(value=classe)
        ttk.Entry(row_frame, textvariable=row_data["numero"], width=8).pack(side="left", padx=2)
        ttk.Entry(row_frame, textvariable=row_data["nom"], width=28).pack(side="left", padx=2)
        ttk.Entry(row_frame, textvariable=row_data["classe"], width=12).pack(side="left", padx=2)
        ttk.Button(row_frame, text="✕", width=3, command=lambda: self._delete_row(row_data)).pack(
            side="left", padx=2)
        self.rows.append(row_data)
        return row_data

    def _delete_row(self, row_data):
        row_data["frame"].destroy()
        self.rows.remove(row_data)

    def _clear_rows(self):
        for row in list(self.rows):
            self._delete_row(row)

    def _load_roster_into_table(self, roster):
        self._clear_rows()
        for num in sorted(roster.keys()):
            info = roster[num]
            self._add_row(num, info.get("nom", ""), info.get("classe", ""))

    def _extract_roster(self):
        roster, errors, seen = {}, [], set()
        for row in self.rows:
            nom = row["nom"].get().strip()
            numero_str = row["numero"].get().strip()
            classe = row["classe"].get().strip()
            if not nom and not numero_str:
                continue
            if not numero_str.isdigit():
                errors.append(tr("rmd_invalid_number_for", name=nom or tr("rmd_no_name")))
                continue
            num = int(numero_str)
            if num in seen:
                errors.append(tr("rmd_number_used_twice", num=num))
                continue
            if not nom:
                errors.append(tr("rmd_name_missing_for", num=num))
                continue
            seen.add(num)
            roster[num] = {"nom": nom, "classe": classe}
        return roster, errors

    # -- Classes saved to memory -----------------------------------------------
    def _refresh_store_combo(self, select=None):
        names = class_archive.list_classes()
        self.store_combo.configure(values=names)
        if select and select in names:
            self.store_combo.set(select)
        elif names and not self.store_combo.get():
            pass

    def _on_load_from_store(self):
        name = self.store_combo.get()
        if not name:
            messagebox.showinfo(APP_TITLE, tr("rmd_choose_class_first"))
            return
        if self.rows and not messagebox.askyesno(APP_TITLE, tr("rmd_confirm_replace_table")):
            return
        roster = class_archive.load_roster(name)
        self._load_roster_into_table(roster)
        self.save_name_var.set(name)

    def _on_rename_in_store(self):
        name = self.store_combo.get()
        if not name:
            messagebox.showinfo(APP_TITLE, tr("rmd_choose_class_first"))
            return
        new_name = simpledialog.askstring(APP_TITLE, tr("rmd_new_name_for", name=name), initialvalue=name,
                                           parent=self)
        if not new_name or new_name.strip() == name:
            return
        new_name = new_name.strip()
        if not class_archive.rename_class(name, new_name):
            messagebox.showerror(APP_TITLE, tr("rmd_rename_target_exists", name=new_name))
            return
        self._refresh_store_combo(select=new_name)
        if self.save_name_var.get() == name:
            self.save_name_var.set(new_name)

    def _on_delete_from_store(self):
        name = self.store_combo.get()
        if not name:
            messagebox.showinfo(APP_TITLE, tr("rmd_choose_class_first"))
            return
        if messagebox.askyesno(APP_TITLE, tr("rmd_confirm_delete_class", name=name)):
            class_archive.delete_class(name)
            self._refresh_store_combo()

    def _on_save_to_store(self):
        roster, errors = self._extract_roster()
        if errors:
            messagebox.showerror(APP_TITLE, tr("rmd_fix_first", errors="\n".join(errors)))
            return
        if not roster:
            messagebox.showwarning(APP_TITLE, tr("rmd_table_empty"))
            return
        name = self.save_name_var.get().strip()
        if not name:
            messagebox.showwarning(APP_TITLE, tr("rmd_name_required"))
            return
        if name in class_archive.list_classes():
            if not messagebox.askyesno(APP_TITLE, tr("gen_class_exists_replace", name=name)):
                return
        class_archive.save_roster(name, roster)
        self._refresh_store_combo(select=name)
        messagebox.showinfo(APP_TITLE, tr("rmd_class_saved", name=name, n=len(roster)))

    # -- Applying to the calling tab -----------------------------
    def _on_use_click(self):
        roster, errors = self._extract_roster()
        if errors:
            messagebox.showerror(APP_TITLE, tr("rmd_fix_first", errors="\n".join(errors)))
            return
        if not roster:
            messagebox.showwarning(APP_TITLE, tr("rmd_table_empty"))
            return
        self.on_apply(roster, self.save_name_var.get().strip() or None)
        self.destroy()


class ArchiveBrowserDialog(tk.Toplevel):
    """Lets the teacher pick a class then one of its archived corrections
    (see class_archive.py) and reload it into ScanTab's results window --
    the "reuse an archived correction" half of the archiving feature (the
    other half, naming + automatically saving one, happens in
    ScanTab._on_run)."""

    def __init__(self, master, on_load):
        super().__init__(master)
        self.title(tr("arch_title"))
        self.geometry("480x420")
        self.on_load = on_load
        self.transient(master)

        top = ttk.Frame(self, padding=12)
        top.pack(fill="both", expand=True)

        ttk.Label(top, text=tr("arch_class_label")).pack(anchor="w")
        self.classe_var = tk.StringVar()
        self.classe_combo = ttk.Combobox(top, textvariable=self.classe_var, state="readonly")
        classes = class_archive.list_classes()
        self.classe_combo["values"] = classes
        self.classe_combo.pack(fill="x", pady=(0, 10))
        self.classe_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_corrections())

        ttk.Label(top, text=tr("arch_correction_label")).pack(anchor="w")
        list_frame = ttk.Frame(top)
        list_frame.pack(fill="both", expand=True, pady=(0, 10))
        self.corr_listbox = tk.Listbox(list_frame)
        self.corr_listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.corr_listbox.yview)
        scroll.pack(side="right", fill="y")
        self.corr_listbox.config(yscrollcommand=scroll.set)
        self.corr_listbox.bind("<Double-1>", lambda e: self._on_load_click())

        if not classes:
            ttk.Label(top, text=tr("arch_no_class"), foreground="#555555",
                      wraplength=420, justify="left").pack(anchor="w", pady=(0, 10))
        else:
            self.classe_combo.current(0)
            self._refresh_corrections()

        btn_row = ttk.Frame(top)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text=tr("arch_load_btn"), command=self._on_load_click).pack(side="left")
        ttk.Button(btn_row, text=tr("btn_close"), command=self.destroy).pack(side="left", padx=5)

    def _refresh_corrections(self):
        self.corr_listbox.delete(0, "end")
        for name in class_archive.list_corrections(self.classe_var.get()):
            self.corr_listbox.insert("end", name)

    def _on_load_click(self):
        classe = self.classe_var.get()
        sel = self.corr_listbox.curselection()
        if not classe or not sel:
            messagebox.showwarning(APP_TITLE, tr("arch_choose_first"))
            return
        run_name = self.corr_listbox.get(sel[0])
        report = class_archive.load_report_json(classe, run_name)
        if report is None:
            messagebox.showerror(APP_TITLE, tr("arch_report_missing"))
            return
        self.on_load(report, classe, run_name)
        self.destroy()


# ---------------------------------------------------------------------
# Tab 2: scanning and grading
# ---------------------------------------------------------------------
class ScanTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=12)
        self.settings = app_config.load_settings()
        self.roster = {}
        self.report = []
        self.answer_key = None
        self.answer_key_name = None
        self.results_win = None
        self.last_run_dir = None
        self.last_run_name = None
        self.last_archive_classe = None
        self.note_out_of_20_var = tk.BooleanVar(value=self.settings.get("note_out_of_20", False))
        self.save_copies_var = tk.BooleanVar(value=self.settings.get("save_scan_copies", False))
        self._build_ui()

    def _build_ui(self):
        s = self.settings

        top = ttk.Frame(self)
        top.pack(fill="x")

        csv_frame = ttk.LabelFrame(top, text=tr("scan_csv_group"), padding=10)
        csv_frame.pack(fill="x", pady=(0, 8))
        self.csv_path_var = tk.StringVar(value=s.get("scan_csv_path", ""))
        row = ttk.Frame(csv_frame)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.csv_path_var, width=50).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text=tr("btn_browse"), command=self._browse_csv).pack(side="left", padx=5)
        ttk.Button(row, text=tr("btn_create_template"), command=lambda: offer_csv_template(self)).pack(
            side="left", padx=5)
        row1b = ttk.Frame(csv_frame)
        row1b.pack(fill="x", pady=(4, 0))
        ttk.Button(row1b, text=tr("gen_roster_table_btn"),
                   command=self._open_roster_manager).pack(side="left")
        ttk.Button(row1b, text=tr("gen_save_class_btn"),
                   command=self._save_class_to_memory).pack(side="left", padx=5)
        self.csv_info_label = ttk.Label(csv_frame, text="", foreground="#555555")
        self.csv_info_label.pack(anchor="w", pady=(4, 0))
        if self.csv_path_var.get() and os.path.exists(self.csv_path_var.get()):
            self._load_csv(self.csv_path_var.get())

        photos_frame = ttk.LabelFrame(top, text=tr("scan_photos_group"), padding=10)
        photos_frame.pack(fill="x", pady=(0, 8))
        btn_row = ttk.Frame(photos_frame)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text=tr("scan_add_photos"), command=self._add_photos).pack(side="left")
        ttk.Button(btn_row, text=tr("scan_add_folder"), command=self._add_folder).pack(side="left", padx=5)
        ttk.Button(btn_row, text=tr("scan_add_pdf"), command=self._add_pdf).pack(
            side="left", padx=5)
        ttk.Button(btn_row, text=tr("scan_remove_selection"), command=self._remove_selected_photos).pack(
            side="left", padx=5)
        ttk.Button(btn_row, text=tr("scan_clear_list"), command=self._clear_photos).pack(side="left", padx=5)
        self.photos_list = tk.Listbox(photos_frame, height=6, selectmode="extended")
        self.photos_list.pack(fill="x", pady=6)

        key_row = ttk.Frame(photos_frame)
        key_row.pack(fill="x")
        ttk.Button(key_row, text=tr("scan_define_key"), command=self._open_answer_key).pack(side="left")
        self.answer_key_label = ttk.Label(key_row, text=tr("scan_key_undefined"),
                                           foreground="#555555")
        self.answer_key_label.pack(side="left", padx=10)

        archive_frame = ttk.LabelFrame(top, text=tr("scan_archive_group"), padding=10)
        archive_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(archive_frame, text=tr("scan_archive_hint"), foreground="#555555",
                  wraplength=760, justify="left").pack(anchor="w")
        ttk.Checkbutton(archive_frame, text=tr("scan_save_copies_checkbox"), variable=self.save_copies_var,
                         command=self._on_toggle_save_copies).pack(anchor="w", pady=(6, 0))

        action_row = ttk.Frame(self)
        action_row.pack(fill="x", pady=6)
        self.run_btn = ttk.Button(action_row, text=tr("scan_btn_run"), command=self._on_run)
        self.run_btn.pack(side="left")
        self.progress = ttk.Progressbar(action_row, mode="indeterminate", length=200)
        self.progress.pack(side="left", padx=10)
        self.show_results_btn = ttk.Button(action_row, text=tr("scan_btn_show_results"),
                                            command=self._ensure_results_window, state="disabled")
        self.show_results_btn.pack(side="left", padx=5)
        self.open_class_archive_btn = ttk.Button(action_row, text=tr("scan_open_class_folder"),
                                                   command=self._open_class_archive_folder, state="disabled")
        self.open_class_archive_btn.pack(side="left", padx=5)
        ttk.Button(action_row, text=tr("scan_load_archive_btn"),
                   command=self._open_archive_browser).pack(side="left", padx=5)

        self.status_label = ttk.Label(self, text="", foreground="#555555")
        self.status_label.pack(fill="x", pady=(4, 0))

    def _ensure_results_window(self):
        """Results (summary + table) are shown in a separate window
        rather than at the bottom of this tab: on a batch of several
        dozen copies, the table didn't fit in the main window and went
        out of view. Closing this window only hides it (it keeps its
        content); 'View results…' reopens it."""
        if self.results_win is not None and self.results_win.winfo_exists():
            self.results_win.deiconify()
            self.results_win.lift()
            return

        win = tk.Toplevel(self)
        win.title(tr("res_title"))
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        win.geometry(f"{min(1000, int(screen_w * 0.8))}x{min(720, int(screen_h * 0.8))}")
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        self.results_win = win

        btn_row = ttk.Frame(win, padding=10)
        btn_row.pack(fill="x")
        self.export_btn = ttk.Button(btn_row, text=tr("res_export_btn"), command=self._on_export)
        self.export_btn.pack(side="left")
        self.update_csv_btn = ttk.Button(btn_row, text=tr("res_update_csv_btn"), command=self._on_update_csv)
        self.update_csv_btn.pack(side="left", padx=5)
        ttk.Checkbutton(btn_row, text=tr("res_note_out_of_20"), variable=self.note_out_of_20_var,
                         command=self._on_toggle_note_mode).pack(side="left", padx=(20, 0))

        self.summary_label = ttk.Label(win, text="", foreground="#333333", wraplength=960, justify="left",
                                        padding=(10, 0))
        self.summary_label.pack(fill="x")

        columns = ("numero", "eleve", "classe", "statut", "lues", "note")
        self.tree = ttk.Treeview(win, columns=columns, show="headings", height=20)
        for col, label, width in [("numero", tr("col_number"), 50), ("eleve", tr("col_name"), 180),
                                   ("classe", tr("col_class"), 80), ("statut", tr("col_status"), 130),
                                   ("lues", tr("col_read"), 70), ("note", tr("col_note"), 90)]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=8)
        self.tree.bind("<Double-1>", self._on_row_double_click)
        self.tree.tag_configure("ok", background="#dff2df")
        self.tree.tag_configure("warn", background="#fdeccb")
        self.tree.tag_configure("unknown", background="#fddede")
        self.tree.tag_configure("missing", background="#e4e9f7")
        self.tree.tag_configure("error", background="#f6c6c6")
        self.tree.tag_configure("read_mismatch", background="#ffcc66")
        ttk.Label(win, text=tr("res_double_click_hint"),
                  foreground="#555555").pack(anchor="w", padx=10, pady=(0, 10))
        ttk.Label(win, text=tr("res_read_mismatch_hint"), foreground="#8a5a00",
                  background="#ffcc66", padding=(6, 3)).pack(anchor="w", padx=10, pady=(0, 10))

        self._populate_tree()

    # -- CSV --------------------------------------------------------
    def _browse_csv(self):
        path = filedialog.askopenfilename(title=tr("csv_choose_file"),
                                           filetypes=[(tr("file_csv"), "*.csv"), (tr("file_all"), "*.*")])
        if path:
            self._load_csv(path)

    def _load_csv(self, path):
        try:
            roster, use_path, message = smart_load_roster_with_export(path)
        except (OSError, KeyError, ValueError) as exc:
            messagebox.showerror(APP_TITLE, tr("csv_read_error", detail=exc))
            return
        if message:
            messagebox.showinfo(APP_TITLE, message)
        self.csv_path_var.set(use_path)
        self.roster = roster
        self.csv_info_label.config(text=tr("csv_loaded_simple", n=len(roster)))

    def _open_roster_manager(self):
        def on_apply(roster, name):
            self.roster = roster
            self.csv_path_var.set(tr("csv_path_from_memory", name=name) if name else tr("csv_path_manual_edit"))
            self.csv_info_label.config(text=tr("csv_loaded_simple", n=len(roster)))

        RosterManagerDialog(self, initial_roster=self.roster,
                             initial_name=suggest_class_name(self.roster) if self.roster else None,
                             on_apply=on_apply)

    def _save_class_to_memory(self):
        roster = self.roster
        if not roster:
            messagebox.showwarning(APP_TITLE, tr("gen_roster_load_warn"))
            return
        suggested = suggest_class_name(roster)
        name = simpledialog.askstring(APP_TITLE, tr("gen_ask_class_name"),
                                       initialvalue=suggested, parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in class_archive.list_classes():
            if not messagebox.askyesno(APP_TITLE, tr("gen_class_exists_replace", name=name)):
                return
        class_archive.save_roster(name, roster)
        messagebox.showinfo(APP_TITLE, tr("gen_class_saved", name=name, n=len(roster)))

    # -- Photos -------------------------------------------------------
    def _add_photos(self):
        paths = filedialog.askopenfilenames(
            title=tr("scan_choose_photos"),
            filetypes=[(tr("file_images"), " ".join(f"*{ext}" for ext in IMAGE_EXTS)), (tr("file_all"), "*.*")])
        for p in paths:
            self.photos_list.insert("end", p)

    def _add_folder(self):
        d = filedialog.askdirectory(title=tr("scan_choose_photo_folder"))
        if not d:
            return
        found = []
        for ext in IMAGE_EXTS:
            found.extend(glob.glob(os.path.join(d, f"*{ext}")))
            found.extend(glob.glob(os.path.join(d, f"*{ext.upper()}")))
        for p in sorted(set(found)):
            self.photos_list.insert("end", p)

    def _add_pdf(self):
        path = filedialog.askopenfilename(title=tr("scan_choose_pdf"),
                                           filetypes=[(tr("file_pdf"), "*.pdf"), (tr("file_all"), "*.*")])
        if not path:
            return
        self.progress.start(12)
        self.status_label.config(text=tr("scan_extracting_pdf", name=os.path.basename(path)))

        def work():
            import fitz
            doc = fitz.open(path)
            try:
                n_pages = doc.page_count
                if n_pages == 0:
                    raise ValueError(tr("scan_pdf_no_pages"))
                # Stamp the extracted page images with the PDF's OWN creation
                # date (rather than "now", when the pages happen to be
                # extracted) so extract_scan_date() can find a meaningful
                # scan date for a PDF-imported batch the same way it does
                # for camera photos (via EXIF).
                pdf_ts = (_parse_pdf_date(doc.metadata.get("creationDate", ""))
                          or _parse_pdf_date(doc.metadata.get("modDate", "")))
                out_dir = tempfile.mkdtemp(prefix="qcm_pdf_")
                mat = fitz.Matrix(300 / 72, 300 / 72)
                page_paths = []
                for i in range(n_pages):
                    pix = doc[i].get_pixmap(matrix=mat)
                    img_path = os.path.join(out_dir, f"page_{i + 1:03d}.png")
                    pix.save(img_path)
                    if pdf_ts:
                        os.utime(img_path, (pdf_ts, pdf_ts))
                    page_paths.append(img_path)
            finally:
                doc.close()
            return page_paths

        def on_done(err, result):
            self.progress.stop()
            self.status_label.config(text="")
            if err:
                exc, tb = err
                messagebox.showerror(APP_TITLE, tr("scan_pdf_read_error", detail=exc))
                return
            for p in result:
                self.photos_list.insert("end", p)
            messagebox.showinfo(APP_TITLE, tr("scan_pdf_extracted", n=len(result), name=os.path.basename(path)))

        run_in_background(self, work, on_done)

    def _remove_selected_photos(self):
        for idx in reversed(self.photos_list.curselection()):
            self.photos_list.delete(idx)

    def _clear_photos(self):
        self.photos_list.delete(0, "end")

    # -- Answer key --------------------------------------------------------
    def _open_answer_key(self):
        s = self.settings
        n_q = s.get("n_questions", 12)
        n_c = s.get("n_choices", 5)
        dialog = AnswerKeyDialog(self, n_q, n_c, existing_key=self.answer_key,
                                  existing_name=self.answer_key_name)
        self.wait_window(dialog)
        if dialog.validated:
            self.answer_key = dialog.result
            self.answer_key_name = dialog.result_name if self.answer_key else None
            s["n_questions"] = int(dialog.n_q_var.get())
            s["n_choices"] = int(dialog.n_choices_var.get())
            app_config.save_settings(s)
            self._update_answer_key_label()

    def _update_answer_key_label(self):
        if self.answer_key and self.answer_key.get("questions"):
            n = len(self.answer_key["questions"])
            total = sum(q.get("points", 1.0) for q in self.answer_key["questions"].values())
            if self.answer_key_name:
                self.answer_key_label.config(
                    text=tr("scan_key_defined_named", name=self.answer_key_name, n=n, total=_fmt_points(total)))
            else:
                self.answer_key_label.config(text=tr("scan_key_defined", n=n, total=_fmt_points(total)))
        else:
            self.answer_key_label.config(text=tr("scan_key_undefined"))

    def _on_toggle_save_copies(self):
        s = self.settings
        s["save_scan_copies"] = self.save_copies_var.get()
        app_config.save_settings(s)

    def _open_class_archive_folder(self):
        if not self.last_archive_classe:
            return
        os.startfile(class_archive.class_dir(self.last_archive_classe))

    def _open_archive_browser(self):
        ArchiveBrowserDialog(self, on_load=self._on_archive_loaded)

    def _on_archive_loaded(self, report, classe_name, run_name):
        self.report = report
        self.last_run_name = run_name
        self.last_archive_classe = classe_name
        self.last_run_dir = class_archive.run_dir(classe_name, run_name)
        self._ensure_results_window()
        self._populate_tree()
        self.show_results_btn.config(state="normal")
        self.open_class_archive_btn.config(state="normal")
        self.summary_label.config(text=tr("arch_loaded_summary", name=run_name, classe=classe_name, n=len(report)))

    # -- Run grading -------------------------------------------
    def _on_run(self):
        photo_paths = list(self.photos_list.get(0, "end"))
        if not photo_paths:
            messagebox.showwarning(APP_TITLE, tr("scan_need_photo"))
            return
        if not self.roster:
            if not messagebox.askyesno(APP_TITLE, tr("scan_no_csv_confirm")):
                return

        # Captured now (main thread) rather than read from work() later:
        # tkinter Variables aren't safe to query from a background thread.
        roster_snapshot = dict(self.roster)
        out_of_20 = self.note_out_of_20_var.get()
        save_copies_wanted = self.save_copies_var.get()

        # Ask for a name for this correction -- pre-filled with a
        # class+date suggestion, editable, so the teacher can label it
        # something meaningful (e.g. "Controle_chapitre_3") to find again
        # later via "Charger une correction archivée…". This also
        # determines the archive folder name (see class_archive.py):
        # there's no separate "working" output folder any more -- the
        # per-class archive IS where a correction's files live, no
        # redundant copy elsewhere.
        classe_name = suggest_run_classe(roster_snapshot)
        suggested_name = build_run_name(roster_snapshot, photo_paths)
        typed_name = simpledialog.askstring(APP_TITLE, tr("scan_ask_run_name"),
                                             initialvalue=suggested_name, parent=self)
        if typed_name is None:
            return
        base_name = _sanitize_filename_part(typed_name.strip()) if typed_name.strip() else suggested_name
        run_name = dedupe_name(base_name, set(class_archive.list_corrections(classe_name)))

        self.run_btn.config(state="disabled")
        self.progress.start(12)
        self._ensure_results_window()
        self.summary_label.config(text=tr("scan_reading_photos", n=len(photo_paths)))

        def work():
            run_dir = class_archive.run_dir(classe_name, run_name)
            report = process_batch(photo_paths, roster_snapshot, run_dir)
            if self.answer_key:
                scoring.compute_scores(report, self.answer_key)

            # Always archive the roster + this run's results under the
            # app's own portable "Données" folder. The scanned copies
            # themselves are only archived (compressed) if the teacher
            # opted in, since that's the heavy part.
            if roster_snapshot:
                class_archive.save_roster(classe_name, roster_snapshot)
            headers, rows = build_results_csv_rows(report, out_of_20)
            class_archive.save_results(classe_name, run_name, headers, rows)
            class_archive.save_report_json(classe_name, run_name, report)
            if save_copies_wanted:
                class_archive.save_copies(classe_name, run_name, report)

            return report, run_dir, run_name, classe_name

        def on_done(err, result):
            self.progress.stop()
            self.run_btn.config(state="normal")
            if err:
                exc, tb = err
                messagebox.showerror(APP_TITLE, tr("scan_failed", detail=exc))
                self.summary_label.config(text="")
                return
            report, run_dir, run_name, classe_name = result
            self.report = report
            self.last_run_dir = run_dir
            self.last_run_name = run_name
            self.last_archive_classe = classe_name
            self._ensure_results_window()
            self._populate_tree()
            self.show_results_btn.config(state="normal")
            self.open_class_archive_btn.config(state="normal")
            s = self.settings
            s["scan_csv_path"] = self.csv_path_var.get()
            app_config.save_settings(s)
            self.summary_label.config(text=format_batch_report(report))

        run_in_background(self, work, on_done)

    def _populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        tag_map = {"ok": "ok", "feuille_douteuse": "warn", "numero_inconnu": "unknown",
                   "face_manquante": "missing", "erreur_lecture": "error"}
        for idx, entry in enumerate(self.report):
            status = entry.get("status", "erreur_lecture")
            numero = entry.get("sheet_number")
            numero = numero if numero is not None else "-"
            eleve = entry.get("eleve", "")
            classe = entry.get("classe", "")
            note_str = format_note(entry, self.note_out_of_20_var.get())

            progress = question_read_progress(entry)
            mismatch = progress is not None and progress[0] != progress[1]
            lues_str = f"{progress[0]}/{progress[1]}" if progress is not None else "-"
            tag = "read_mismatch" if mismatch else tag_map.get(status, "error")

            self.tree.insert("", "end", iid=str(idx),
                              values=(numero, eleve, classe, status_label(status), lues_str, note_str),
                              tags=(tag,))

    def _on_toggle_note_mode(self):
        s = self.settings
        s["note_out_of_20"] = self.note_out_of_20_var.get()
        app_config.save_settings(s)
        self._populate_tree()

    def _on_row_double_click(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        entry = self.report[idx]

        def on_saved(updated_entry):
            self.report[idx] = updated_entry
            if self.answer_key:
                scoring.rescore_entry(updated_entry, self.answer_key)
            self._populate_tree()

        if entry.get("status") == "erreur_lecture":
            default_n_q = self.settings.get("n_questions", 12)
            default_n_c = self.settings.get("n_choices", 5)
            ManualEntryDialog(self, entry, self.roster, default_n_q, default_n_c, on_saved)
            return

        DetailDialog(self, entry, self.roster, on_saved)

    # -- Export -----------------------------------------------------
    def _on_export(self):
        if not self.report:
            return
        default_dir = self.last_run_dir or app_config.get_config_dir()
        default_name = self.last_run_name or ("resultats_" + time.strftime("%Y%m%d_%H%M%S"))
        path = filedialog.asksaveasfilename(
            title=tr("res_export_title"), initialdir=default_dir,
            initialfile=f"{default_name}.csv",
            defaultextension=".csv", filetypes=[(tr("file_csv"), "*.csv")])
        if not path:
            return
        headers, rows = build_results_csv_rows(self.report, self.note_out_of_20_var.get())
        write_results_csv(path, headers, rows)
        if messagebox.askyesno(APP_TITLE, tr("res_exported", path=path)):
            os.startfile(path)

    def _on_update_csv(self):
        path = self.csv_path_var.get()
        # csv_path_var sometimes shows an informational text rather than
        # a real path (e.g. "(class from memory: 6A)" after loading from
        # memory): never use it as-is as a file to write, always ask for
        # a location in that case.
        if not path or not path.lower().endswith(".csv"):
            path = filedialog.asksaveasfilename(title=tr("res_update_csv_title"), defaultextension=".csv",
                                                 filetypes=[(tr("file_csv"), "*.csv")])
            if not path:
                return
        merged = dict(self.roster)
        for e in self.report:
            if e.get("eleve") and e.get("sheet_number") is not None:
                merged[e["sheet_number"]] = {"nom": e["eleve"], "classe": e.get("classe", "")}
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["numero", "nom", "classe"])
                for num in sorted(merged.keys()):
                    w.writerow([num, merged[num]["nom"], merged[num].get("classe", "")])
        except OSError as exc:
            messagebox.showerror(APP_TITLE, tr("res_csv_save_error", detail=exc))
            return
        self.roster = merged
        self.csv_path_var.set(path)
        self.csv_info_label.config(text=tr("csv_loaded_simple", n=len(merged)))
        messagebox.showinfo(APP_TITLE, tr("res_csv_updated", path=path))


# ---------------------------------------------------------------------
# Tab 3: reviewing/editing/analyzing the saved per-class archive
# (class_archive.py) -- roster, and the history of past corrections.
# ---------------------------------------------------------------------
class DataTab(ttk.Frame):
    """Trois modes, choisis par les boutons radio du haut : les listes
    d'élèves (classes) enregistrées, les corrections archivées, et les
    corrigés/barèmes de QCM réutilisables (voir answer_key_store.py). Les
    deux premiers modes partagent le sélecteur de classe en haut de
    l'onglet ; le troisième n'en a pas besoin (les corrigés ne sont pas
    liés à une classe en particulier)."""

    def __init__(self, master, on_load_correction):
        super().__init__(master, padding=12)
        self.on_load_correction = on_load_correction
        self.current_classe = None
        self.mode_var = tk.StringVar(value="classes")
        self._build_ui()
        self._refresh_classes()
        self._refresh_keys()

    def _build_ui(self):
        mode_frame = ttk.Frame(self)
        mode_frame.pack(fill="x", pady=(0, 8))
        modes = [
            ("classes", tr("data_mode_classes")),
            ("corrections", tr("data_mode_corrections")),
            ("keys", tr("data_mode_keys")),
        ]
        for value, label in modes:
            ttk.Radiobutton(mode_frame, text=label, variable=self.mode_var, value=value,
                            command=self._on_mode_change).pack(side="left", padx=(0, 15))

        self.classe_row = ttk.Frame(self)
        ttk.Label(self.classe_row, text=tr("data_class_label")).pack(side="left")
        self.classe_var = tk.StringVar()
        self.classe_combo = ttk.Combobox(self.classe_row, textvariable=self.classe_var, state="readonly", width=25)
        self.classe_combo.pack(side="left", padx=5)
        self.classe_combo.bind("<<ComboboxSelected>>", lambda e: self._load_classe())
        ttk.Button(self.classe_row, text=tr("btn_refresh"), command=self._refresh_classes).pack(side="left", padx=5)
        self.open_folder_btn = ttk.Button(self.classe_row, text=tr("scan_open_class_folder"),
                                           command=self._open_folder, state="disabled")
        self.open_folder_btn.pack(side="left", padx=5)

        self.summary_label = ttk.Label(self, text="", foreground="#333333", wraplength=760, justify="left")

        # --- Mode : listes d'élèves (classes) ---
        self.classes_mode_frame = ttk.Frame(self)

        new_class_frame = ttk.LabelFrame(self.classes_mode_frame, text=tr("data_new_class_group"), padding=10)
        new_class_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(new_class_frame, text=tr("data_new_class_names_hint")).pack(anchor="w")
        self.new_class_names_text = tk.Text(new_class_frame, height=6, width=45)
        self.new_class_names_text.pack(fill="x", pady=3)
        new_class_row = ttk.Frame(new_class_frame)
        new_class_row.pack(fill="x")
        ttk.Label(new_class_row, text=tr("data_new_class_classe_label")).pack(side="left")
        self.new_class_classe_var = tk.StringVar()
        ttk.Entry(new_class_row, textvariable=self.new_class_classe_var, width=15).pack(side="left", padx=5)
        ttk.Button(new_class_row, text=tr("data_new_class_btn"), command=self._create_class).pack(
            side="left", padx=5)

        roster_frame = ttk.LabelFrame(self.classes_mode_frame, text=tr("data_roster_group"), padding=10)
        roster_frame.pack(fill="both", expand=True)
        self.roster_editor = RosterTableEditor(roster_frame, height=180)
        self.roster_editor.pack(fill="both", expand=True)
        ttk.Button(roster_frame, text=tr("data_save_roster_btn"), command=self._save_roster).pack(
            anchor="w", pady=(6, 0))

        # --- Mode : corrections enregistrées ---
        self.corrections_mode_frame = ttk.Frame(self)
        corr_frame = ttk.LabelFrame(self.corrections_mode_frame, text=tr("data_corrections_group"), padding=10)
        corr_frame.pack(fill="both", expand=True)
        columns = ("nom", "date", "eleves", "moyenne")
        self.corr_tree = ttk.Treeview(corr_frame, columns=columns, show="headings", height=8)
        for col, label, width in [("nom", tr("data_col_correction"), 220), ("date", tr("data_col_date"), 130),
                                   ("eleves", tr("data_col_copies"), 80), ("moyenne", tr("data_col_average"), 100)]:
            self.corr_tree.heading(col, text=label)
            self.corr_tree.column(col, width=width, anchor="w")
        self.corr_tree.pack(fill="both", expand=True)
        self.corr_tree.bind("<Double-1>", lambda e: self._load_selected_correction())
        corr_btn_row = ttk.Frame(corr_frame)
        corr_btn_row.pack(fill="x", pady=(6, 0))
        ttk.Button(corr_btn_row, text=tr("data_load_correction_btn"), command=self._load_selected_correction).pack(
            side="left")
        ttk.Button(corr_btn_row, text=tr("data_delete_correction_btn"),
                   command=self._delete_selected_correction).pack(side="left", padx=5)

        # --- Mode : corrigés et barèmes de QCM ---
        self.keys_mode_frame = ttk.Frame(self)
        keys_frame = ttk.LabelFrame(self.keys_mode_frame, text=tr("data_keys_group"), padding=10)
        keys_frame.pack(fill="both", expand=True)
        key_columns = ("nom", "questions", "points")
        self.keys_tree = ttk.Treeview(keys_frame, columns=key_columns, show="headings", height=10)
        for col, label, width in [("nom", tr("data_col_key_name"), 260),
                                   ("questions", tr("data_col_key_questions"), 140),
                                   ("points", tr("data_col_key_points"), 140)]:
            self.keys_tree.heading(col, text=label)
            self.keys_tree.column(col, width=width, anchor="w")
        self.keys_tree.pack(fill="both", expand=True)
        self.keys_tree.bind("<Double-1>", lambda e: self._open_selected_key())
        keys_btn_row = ttk.Frame(keys_frame)
        keys_btn_row.pack(fill="x", pady=(6, 0))
        ttk.Button(keys_btn_row, text=tr("data_keys_new_btn"), command=self._new_key).pack(side="left")
        ttk.Button(keys_btn_row, text=tr("data_keys_open_btn"), command=self._open_selected_key).pack(
            side="left", padx=5)
        ttk.Button(keys_btn_row, text=tr("data_keys_rename_btn"), command=self._rename_selected_key).pack(
            side="left", padx=5)
        ttk.Button(keys_btn_row, text=tr("data_keys_delete_btn"), command=self._delete_selected_key).pack(
            side="left", padx=5)

        self._on_mode_change()

    def _on_mode_change(self):
        mode = self.mode_var.get()
        self.classe_row.pack_forget()
        self.summary_label.pack_forget()
        self.classes_mode_frame.pack_forget()
        self.corrections_mode_frame.pack_forget()
        self.keys_mode_frame.pack_forget()
        if mode == "classes":
            self.classe_row.pack(fill="x")
            self.summary_label.pack(fill="x", pady=(8, 8))
            self.classes_mode_frame.pack(fill="both", expand=True)
        elif mode == "corrections":
            self.classe_row.pack(fill="x")
            self.summary_label.pack(fill="x", pady=(8, 8))
            self.corrections_mode_frame.pack(fill="both", expand=True)
        else:
            self.keys_mode_frame.pack(fill="both", expand=True)
            self._refresh_keys()

    # -- Classes -----------------------------------------------------
    def _refresh_classes(self, select=None):
        classes = class_archive.list_classes()
        selected = select or self.classe_var.get()
        self.classe_combo["values"] = classes
        if not classes:
            self.classe_var.set("")
            self.current_classe = None
            self.roster_editor.clear_rows()
            self.corr_tree.delete(*self.corr_tree.get_children())
            self.open_folder_btn.config(state="disabled")
            self.summary_label.config(text=tr("data_no_class"))
            return
        if selected in classes:
            self.classe_combo.set(selected)
        else:
            self.classe_combo.current(0)
        self._load_classe()

    def _create_class(self):
        raw = self.new_class_names_text.get("1.0", "end").splitlines()
        names = [n.strip() for n in raw if n.strip()]
        if not names:
            messagebox.showwarning(APP_TITLE, tr("gen_names_required"))
            return
        classe = self.new_class_classe_var.get().strip()
        if not classe:
            messagebox.showwarning(APP_TITLE, tr("data_new_class_name_required"))
            return
        if classe in class_archive.list_classes():
            if not messagebox.askyesno(APP_TITLE, tr("gen_class_exists_replace", name=classe)):
                return
        roster = {i + 1: {"nom": name, "classe": classe} for i, name in enumerate(names)}
        class_archive.save_roster(classe, roster)
        messagebox.showinfo(APP_TITLE, tr("gen_class_saved", name=classe, n=len(roster)))
        self.new_class_names_text.delete("1.0", "end")
        self.new_class_classe_var.set("")
        self._refresh_classes(select=classe)

    def _load_classe(self):
        classe = self.classe_var.get()
        self.current_classe = classe
        self.open_folder_btn.config(state="normal")
        self.roster_editor.load_roster(class_archive.load_roster(classe))
        self._refresh_corrections()

    def _open_folder(self):
        if self.current_classe:
            os.startfile(class_archive.class_dir(self.current_classe))

    # -- Roster (retoucher / compléter) -------------------------------
    def _save_roster(self):
        if not self.current_classe:
            return
        roster, errors = self.roster_editor.extract_roster()
        if errors:
            messagebox.showerror(APP_TITLE, tr("rmd_fix_first", errors="\n".join(errors)))
            return
        class_archive.save_roster(self.current_classe, roster)
        messagebox.showinfo(APP_TITLE, tr("data_roster_saved", n=len(roster)))
        self._refresh_corrections()

    # -- Corrections (vérifier / analyser) ----------------------------
    def _refresh_corrections(self):
        self.corr_tree.delete(*self.corr_tree.get_children())
        classe = self.current_classe
        if not classe:
            return
        names = class_archive.list_corrections(classe)
        averages = []
        for name in names:
            stats = class_archive.correction_stats(classe, name) or {}
            n_entries = stats.get("n_entries")
            avg = stats.get("avg_note_20")
            if avg is not None:
                averages.append(avg)
            self.corr_tree.insert(
                "", "end", iid=name,
                values=(name, stats.get("date", ""), n_entries if n_entries is not None else "-",
                        f"{avg:.1f}/20" if avg is not None else "-"))
        n_students = len(class_archive.load_roster(classe))
        overall = f"{sum(averages) / len(averages):.1f}/20" if averages else "-"
        self.summary_label.config(text=tr("data_class_summary", n_students=n_students,
                                           n_corrections=len(names), overall=overall))

    def _load_selected_correction(self):
        classe = self.current_classe
        sel = self.corr_tree.selection()
        if not classe or not sel:
            messagebox.showwarning(APP_TITLE, tr("data_choose_correction_first"))
            return
        run_name = sel[0]
        report = class_archive.load_report_json(classe, run_name)
        if report is None:
            messagebox.showerror(APP_TITLE, tr("arch_report_missing"))
            return
        self.on_load_correction(report, classe, run_name)

    def _delete_selected_correction(self):
        classe = self.current_classe
        sel = self.corr_tree.selection()
        if not classe or not sel:
            messagebox.showwarning(APP_TITLE, tr("data_choose_correction_first"))
            return
        run_name = sel[0]
        if messagebox.askyesno(APP_TITLE, tr("data_confirm_delete_correction", name=run_name)):
            class_archive.delete_correction(classe, run_name)
            self._refresh_corrections()

    # -- Corrigés et barèmes de QCM ------------------------------------
    def _refresh_keys(self):
        self.keys_tree.delete(*self.keys_tree.get_children())
        for name in answer_key_store.list_keys():
            key = answer_key_store.load_key(name) or {"questions": {}}
            questions = key.get("questions", {})
            total_points = sum(info.get("points", 0) for info in questions.values())
            self.keys_tree.insert("", "end", iid=name, values=(name, len(questions), _fmt_points(total_points)))

    def _selected_key_name(self):
        sel = self.keys_tree.selection()
        if not sel:
            messagebox.showwarning(APP_TITLE, tr("data_choose_key_first"))
            return None
        return sel[0]

    def _new_key(self):
        settings = app_config.load_settings()
        n_q = settings.get("n_questions", 12)
        n_c = settings.get("n_choices", 5)
        dialog = AnswerKeyDialog(self, n_q, n_c)
        self.wait_window(dialog)
        self._refresh_keys()

    def _open_selected_key(self):
        name = self._selected_key_name()
        if not name:
            return
        key = answer_key_store.load_key(name)
        if key is None:
            messagebox.showerror(APP_TITLE, tr("arch_report_missing"))
            return
        questions = key.get("questions", {})
        n_q = max(questions.keys(), default=12)
        n_c = max((len(info.get("correct", [])) for info in questions.values()), default=3)
        n_c = max(3, min(6, n_c))
        dialog = AnswerKeyDialog(self, n_q, n_c, existing_key=key, existing_name=name)
        self.wait_window(dialog)
        self._refresh_keys()

    def _rename_selected_key(self):
        name = self._selected_key_name()
        if not name:
            return
        new_name = simpledialog.askstring(APP_TITLE, tr("ak_new_name_for", name=name), initialvalue=name,
                                           parent=self)
        if not new_name or new_name.strip() == name:
            return
        new_name = new_name.strip()
        if not answer_key_store.rename_key(name, new_name):
            messagebox.showerror(APP_TITLE, tr("ak_rename_target_exists", name=new_name))
            return
        self._refresh_keys()

    def _delete_selected_key(self):
        name = self._selected_key_name()
        if not name:
            return
        if messagebox.askyesno(APP_TITLE, tr("ak_confirm_delete", name=name)):
            answer_key_store.delete_key(name)
            self._refresh_keys()


# ---------------------------------------------------------------------
# Tab 4: preferences (language)
# ---------------------------------------------------------------------
class PreferencesTab(ttk.Frame):
    def __init__(self, master, on_language_change):
        super().__init__(master, padding=12)
        self.on_language_change = on_language_change

        lang_frame = ttk.LabelFrame(self, text=tr("prefs_group"), padding=10)
        lang_frame.pack(fill="x", pady=(0, 10))
        row = ttk.Frame(lang_frame)
        row.pack(fill="x")
        ttk.Label(row, text=tr("prefs_language_label")).pack(side="left")
        self._label_to_code = {label: code for code, label in translations.LANGUAGES.items()}
        current_label = translations.LANGUAGES[translations.get_language()]
        self.lang_var = tk.StringVar(value=current_label)
        combo = ttk.Combobox(row, textvariable=self.lang_var, values=list(translations.LANGUAGES.values()),
                              state="readonly", width=20)
        combo.pack(side="left", padx=5)
        combo.bind("<<ComboboxSelected>>", self._on_language_selected)
        ttk.Label(lang_frame, text=tr("prefs_hint"), foreground="#555555",
                  wraplength=620, justify="left").pack(anchor="w", pady=(6, 0))

        about_frame = ttk.LabelFrame(self, text=tr("prefs_about_group"), padding=10)
        about_frame.pack(fill="x")
        ttk.Label(about_frame, text=tr("prefs_about_text"), justify="left",
                  wraplength=620, foreground="#555555").pack(anchor="w")

    def _on_language_selected(self, event):
        code = self._label_to_code.get(self.lang_var.get())
        if not code or code == translations.get_language():
            return
        translations.set_language(code)
        s = app_config.load_settings()
        s["language"] = code
        app_config.save_settings(s)
        self.on_language_change()


def main():
    translations.load_language_from_settings()
    class_archive.migrate_legacy_class_store()
    answer_key_store.migrate_legacy_store()
    root = tk.Tk()
    root.geometry("880x820")

    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass

    header = ttk.Frame(root, padding=(12, 10, 12, 0))
    header.pack(fill="x")
    title_label = ttk.Label(header, font=("Segoe UI", 14, "bold"))
    title_label.pack(anchor="w")
    subtitle_label = ttk.Label(header, foreground="#555555")
    subtitle_label.pack(anchor="w")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=12, pady=10)

    def rebuild_ui():
        root.title(tr("app_title"))
        title_label.config(text=tr("app_title"))
        subtitle_label.config(text=tr("app_subtitle"))
        for child in notebook.winfo_children():
            child.destroy()
        scan_tab = ScanTab(notebook)

        def on_load_correction(report, classe, run_name):
            scan_tab._on_archive_loaded(report, classe, run_name)
            notebook.select(scan_tab)

        notebook.add(GenerateTab(notebook), text=tr("tab_generate"))
        notebook.add(scan_tab, text=tr("tab_scan"))
        notebook.add(DataTab(notebook, on_load_correction=on_load_correction), text=tr("tab_data"))
        notebook.add(PreferencesTab(notebook, on_language_change=rebuild_ui), text=tr("tab_preferences"))

    rebuild_ui()
    root.mainloop()


if __name__ == "__main__":
    main()

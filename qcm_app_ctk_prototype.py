"""Experimental prototype: what "Scanner / Corriger" could look like
rewritten in CustomTkinter instead of plain ttk (see PROTOTYPE_README.md
for context, scope, and trade-offs -- this is NOT a replacement for
qcm_app.py, only a design exploration on its own branch).

Deliberately reuses every bit of EXISTING business logic unchanged --
roster_match.process_batch, class_archive.*, scoring.*, app_config.*,
translations.tr(), and even several small UI-adjacent helpers already
written for qcm_app.py's ScanTab (CSV loading, PDF extraction, rotation,
run-name suggestions...) are imported directly from qcm_app, not
duplicated. Only the WIDGET layer (what you see on screen) is new: this
keeps the comparison fair -- same features, same data, same correction
engine, different look -- and means nothing here can drift out of sync
with how a real correction actually behaves.

Two widgets CustomTkinter simply doesn't provide a replacement for yet
(a listbox and a sortable, multi-column table) stay as plain tk/ttk
widgets, colour-matched to the rest of the window -- see
PROTOTYPE_README.md for what a full native version would require.
"""
import glob
import os
import sys
import tempfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import customtkinter as ctk
from PIL import Image

import app_config
import class_archive
import scoring
import translations
from translations import tr
from roster_match import process_batch, format_batch_report
from qcm_app import (
    APP_TITLE,
    IMAGE_EXTS,
    status_label,
    format_note,
    build_results_csv_rows,
    question_read_progress,
    smart_load_roster_with_export,
    suggest_class_name,
    suggest_run_classe,
    build_run_name,
    dedupe_name,
    _sanitize_filename_part,
    _apply_rotation,
    _materialize_rotations,
    run_in_background,
)

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

ACCENT = "#2f6fed"
ROTATED_TINT = "#dbe8ff"
LIST_BG = "#ffffff"


class ScanTabCTk(ctk.CTkFrame):
    """CustomTkinter rewrite of qcm_app.ScanTab -- same fields/behaviour
    (self.roster, self.report, self.photo_rotations...), new widgets."""

    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.settings = app_config.load_settings()
        self.roster = {}
        self.scan_current_classe = None
        self.report = []
        self.answer_key = None
        self.last_run_dir = None
        self.last_run_name = None
        self.last_archive_classe = None
        self.photo_rotations = {}
        self.results_win = None
        self._preview_image_path = None
        self._preview_ctk_image = None
        self.note_out_of_20_var = tk.BooleanVar(value=self.settings.get("note_out_of_20", False))
        self.save_copies_var = tk.BooleanVar(value=self.settings.get("save_scan_copies", False))
        self._build_ui()

    def _build_ui(self):
        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        # --- 1. Classe ---
        class_card = ctk.CTkFrame(scroll, corner_radius=12)
        class_card.pack(fill="x", padx=4, pady=(4, 10))
        ctk.CTkLabel(class_card, text=tr("scan_csv_group"),
                     font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=14, pady=(12, 6))

        row = ctk.CTkFrame(class_card, fg_color="transparent")
        row.pack(fill="x", padx=14)
        ctk.CTkLabel(row, text=tr("data_class_label")).pack(side="left")
        self.scan_classe_var = tk.StringVar()
        self.scan_classe_combo = ctk.CTkComboBox(row, variable=self.scan_classe_var, width=220,
                                                   command=lambda _v: self._load_scan_classe())
        self.scan_classe_combo.pack(side="left", padx=8)
        ctk.CTkButton(row, text=tr("btn_refresh"), width=110,
                      command=self._refresh_scan_classes).pack(side="left", padx=4)
        ctk.CTkButton(row, text=tr("scan_load_csv_btn"), width=160,
                      command=self._load_class_from_csv).pack(side="left", padx=4)

        self.roster_box = ctk.CTkTextbox(class_card, height=110, fg_color=LIST_BG)
        self.roster_box.pack(fill="x", padx=14, pady=10)
        self.roster_box.configure(state="disabled")
        self._refresh_scan_classes(select=self.settings.get("scan_last_classe", ""))

        # --- 2. Photos ---
        photos_card = ctk.CTkFrame(scroll, corner_radius=12)
        photos_card.pack(fill="x", padx=4, pady=(0, 10))
        ctk.CTkLabel(photos_card, text=tr("scan_photos_group"),
                     font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=14, pady=(12, 6))

        btn_row = ctk.CTkFrame(photos_card, fg_color="transparent")
        btn_row.pack(fill="x", padx=14)
        ctk.CTkButton(btn_row, text=tr("scan_add_photos"), command=self._add_photos).pack(side="left")
        ctk.CTkButton(btn_row, text=tr("scan_add_folder"), command=self._add_folder).pack(
            side="left", padx=6)
        ctk.CTkButton(btn_row, text=tr("scan_add_pdf"), command=self._add_pdf).pack(side="left")
        ctk.CTkButton(btn_row, text=tr("scan_remove_selection"), fg_color="#b23b3b",
                      hover_color="#8f2e2e", command=self._remove_selected_photos).pack(
            side="left", padx=6)
        ctk.CTkButton(btn_row, text=tr("scan_clear_list"), fg_color="#b23b3b",
                      hover_color="#8f2e2e", command=self._clear_photos).pack(side="left")

        rotate_row = ctk.CTkFrame(photos_card, fg_color="transparent")
        rotate_row.pack(fill="x", padx=14, pady=(8, 0))
        ctk.CTkLabel(rotate_row, text=tr("scan_rotate_label")).pack(side="left")
        ctk.CTkButton(rotate_row, text=tr("scan_rotate_90"), width=120, fg_color="#5a5a5a",
                      hover_color="#404040", command=lambda: self._rotate_selected(90)).pack(
            side="left", padx=6)
        ctk.CTkButton(rotate_row, text=tr("scan_rotate_180"), width=120, fg_color="#5a5a5a",
                      hover_color="#404040", command=lambda: self._rotate_selected(180)).pack(
            side="left")

        rv_row = ctk.CTkFrame(photos_card, fg_color="transparent")
        rv_row.pack(fill="x", padx=14, pady=(8, 0))
        ctk.CTkLabel(rv_row, text=tr("scan_rv_label")).pack(side="left")
        ctk.CTkButton(rv_row, text=tr("scan_ignore_rectos"), width=140, fg_color="#5a5a5a",
                      hover_color="#404040", command=self._ignore_rectos).pack(side="left", padx=6)
        ctk.CTkButton(rv_row, text=tr("scan_ignore_versos"), width=140, fg_color="#5a5a5a",
                      hover_color="#404040", command=self._ignore_versos).pack(side="left")

        list_row = ctk.CTkFrame(photos_card, fg_color="transparent")
        list_row.pack(fill="x", padx=14, pady=10)
        list_frame = ctk.CTkFrame(list_row, corner_radius=10, fg_color=LIST_BG)
        list_frame.pack(side="left", fill="both", expand=True)
        self.photos_list = tk.Listbox(list_frame, height=10, selectmode="extended",
                                       exportselection=False, relief="flat", borderwidth=0,
                                       background=LIST_BG, highlightthickness=0)
        self.photos_list.pack(fill="both", expand=True, padx=8, pady=8)
        self.photos_list.bind("<<ListboxSelect>>", self._on_photo_select)
        self.photos_list.bind("<Double-Button-1>", self._on_photo_double_click)
        self._listbox_default_bg = LIST_BG

        preview_frame = ctk.CTkFrame(list_row, corner_radius=10, width=240, height=280,
                                      fg_color="#ececec")
        preview_frame.pack(side="left", padx=(10, 0))
        preview_frame.pack_propagate(False)
        self.preview_label = ctk.CTkLabel(preview_frame, text=tr("scan_preview_empty"),
                                           wraplength=200, justify="center", text_color="#666666")
        self.preview_label.pack(fill="both", expand=True)
        self.preview_label.bind("<Double-Button-1>", self._on_photo_double_click)

        ctk.CTkLabel(photos_card, text=tr("scan_preview_hint"), text_color="#666666").pack(
            anchor="w", padx=14)
        ctk.CTkLabel(photos_card, text=tr("scan_rotate_hint"), text_color="#666666",
                     wraplength=760, justify="left").pack(anchor="w", padx=14, pady=(2, 0))
        ctk.CTkLabel(photos_card, text=tr("scan_rv_hint"), text_color="#666666",
                     wraplength=760, justify="left").pack(anchor="w", padx=14, pady=(2, 12))

        # --- Answer key ---
        key_row = ctk.CTkFrame(photos_card, fg_color="transparent")
        key_row.pack(fill="x", padx=14, pady=(0, 12))
        self.answer_key_label = ctk.CTkLabel(key_row, text=tr("scan_key_undefined"),
                                              text_color="#666666")
        self.answer_key_label.pack(side="left")

        # --- 3. Archive ---
        archive_card = ctk.CTkFrame(scroll, corner_radius=12)
        archive_card.pack(fill="x", padx=4, pady=(0, 10))
        ctk.CTkLabel(archive_card, text=tr("scan_archive_group"),
                     font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=14, pady=(12, 6))
        self.save_copies_check = ctk.CTkCheckBox(archive_card, text=tr("scan_save_copies_checkbox"),
                                                   variable=self.save_copies_var,
                                                   command=self._on_toggle_save_copies)
        self.save_copies_check.pack(anchor="w", padx=14, pady=(0, 12))

        # --- Actions ---
        action_row = ctk.CTkFrame(scroll, fg_color="transparent")
        action_row.pack(fill="x", padx=4, pady=(0, 10))
        self.run_btn = ctk.CTkButton(action_row, text=tr("scan_btn_run"), fg_color=ACCENT,
                                      hover_color="#1f52c0", command=self._on_run)
        self.run_btn.pack(side="left")
        self.progress = ctk.CTkProgressBar(action_row, mode="indeterminate", width=200)
        self.progress.pack(side="left", padx=12)
        self.show_results_btn = ctk.CTkButton(action_row, text=tr("scan_btn_show_results"),
                                               state="disabled", command=self._ensure_results_window)
        self.show_results_btn.pack(side="left", padx=6)

        self.status_label = ctk.CTkLabel(scroll, text="", text_color="#666666")
        self.status_label.pack(fill="x", padx=4)

    # -- CSV / roster --------------------------------------------------
    def _refresh_scan_classes(self, select=None):
        classes = class_archive.list_classes()
        self.scan_classe_combo.configure(values=classes)
        if not classes:
            self.scan_classe_var.set("")
            self.scan_current_classe = None
            self._show_roster({})
            self.roster = {}
            return
        chosen = select if select in classes else classes[0]
        self.scan_classe_var.set(chosen)
        self._load_scan_classe()

    def _load_scan_classe(self):
        classe = self.scan_classe_var.get()
        self.scan_current_classe = classe
        self.roster = class_archive.load_roster(classe) if classe else {}
        self._show_roster(self.roster)

    def _show_roster(self, roster):
        self.roster_box.configure(state="normal")
        self.roster_box.delete("1.0", "end")
        if not roster:
            self.roster_box.insert("1.0", tr("scan_roster_empty"))
        else:
            lines = [f"n°{num} — {info['nom']}" for num, info in sorted(roster.items())]
            self.roster_box.insert("1.0", "\n".join(lines))
        self.roster_box.configure(state="disabled")

    def _load_class_from_csv(self):
        path = filedialog.askopenfilename(title=tr("csv_choose_file"),
                                           filetypes=[(tr("file_csv"), "*.csv"), (tr("file_all"), "*.*")])
        if not path:
            return
        try:
            roster, _use_path, message = smart_load_roster_with_export(path)
        except (OSError, KeyError, ValueError) as exc:
            messagebox.showerror(APP_TITLE, tr("csv_read_error", detail=exc))
            return
        if message:
            messagebox.showinfo(APP_TITLE, message)
        if not roster:
            return
        suggested = suggest_class_name(roster)
        name = simpledialog.askstring(APP_TITLE, tr("gen_ask_class_name"), initialvalue=suggested, parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        class_archive.save_roster(name, roster)
        messagebox.showinfo(APP_TITLE, tr("gen_class_saved", name=name, n=len(roster)))
        self._refresh_scan_classes(select=name)

    # -- Photos ----------------------------------------------------------
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
        self.progress.start()
        self.status_label.configure(text=tr("scan_extracting_pdf", name=os.path.basename(path)))

        def work():
            import fitz
            doc = fitz.open(path)
            try:
                n_pages = doc.page_count
                if n_pages == 0:
                    raise ValueError(tr("scan_pdf_no_pages"))
                out_dir = tempfile.mkdtemp(prefix="qcm_pdf_")
                mat = fitz.Matrix(300 / 72, 300 / 72)
                page_paths = []
                for i in range(n_pages):
                    pix = doc[i].get_pixmap(matrix=mat)
                    img_path = os.path.join(out_dir, f"page_{i + 1:03d}.png")
                    pix.save(img_path)
                    page_paths.append(img_path)
            finally:
                doc.close()
            return page_paths

        def on_done(err, result):
            self.progress.stop()
            self.status_label.configure(text="")
            if err:
                exc, _tb = err
                messagebox.showerror(APP_TITLE, tr("scan_pdf_read_error", detail=exc))
                return
            for p in result:
                self.photos_list.insert("end", p)
            messagebox.showinfo(APP_TITLE, tr("scan_pdf_extracted", n=len(result), name=os.path.basename(path)))

        run_in_background(self, work, on_done)

    def _remove_photo_indices(self, indices):
        indices = sorted(set(indices))
        removed = {self.photos_list.get(idx) for idx in indices}
        for idx in reversed(indices):
            self.photos_list.delete(idx)
        for path in removed:
            self.photo_rotations.pop(path, None)
        if self._preview_image_path in removed:
            self._clear_preview()

    def _remove_selected_photos(self):
        self._remove_photo_indices(self.photos_list.curselection())

    def _clear_photos(self):
        self.photos_list.delete(0, "end")
        self.photo_rotations.clear()
        self._clear_preview()

    def _ignore_rectos(self):
        n = self.photos_list.size()
        if n == 0:
            messagebox.showwarning(APP_TITLE, tr("scan_rv_empty_list"))
            return
        self._remove_photo_indices(range(0, n, 2))

    def _ignore_versos(self):
        n = self.photos_list.size()
        if n == 0:
            messagebox.showwarning(APP_TITLE, tr("scan_rv_empty_list"))
            return
        self._remove_photo_indices(range(1, n, 2))

    def _rotate_selected(self, delta):
        sel = self.photos_list.curselection()
        if not sel:
            messagebox.showwarning(APP_TITLE, tr("scan_rotate_need_selection"))
            return
        for idx in sel:
            path = self.photos_list.get(idx)
            new_deg = (self.photo_rotations.get(path, 0) + delta) % 360
            if new_deg:
                self.photo_rotations[path] = new_deg
                self.photos_list.itemconfig(idx, background=ROTATED_TINT)
            else:
                self.photo_rotations.pop(path, None)
                self.photos_list.itemconfig(idx, background=self._listbox_default_bg)
        if self._preview_image_path in {self.photos_list.get(idx) for idx in sel}:
            self._show_preview(self._preview_image_path)

    # -- Preview ----------------------------------------------------------
    def _on_photo_select(self, _event):
        sel = self.photos_list.curselection()
        if not sel:
            return
        self._show_preview(self.photos_list.get(sel[0]))

    def _show_preview(self, path):
        self._preview_image_path = path
        try:
            with Image.open(path) as img:
                img = img.convert("RGB")
                img = _apply_rotation(img, self.photo_rotations.get(path, 0))
                img.thumbnail((220, 260), Image.LANCZOS)
                ctk_img = ctk.CTkImage(light_image=img, size=img.size)
        except (OSError, ValueError) as exc:
            self._preview_ctk_image = None
            self.preview_label.configure(image=None, text=tr("scan_preview_load_error", detail=exc))
            return
        self._preview_ctk_image = ctk_img
        self.preview_label.configure(image=ctk_img, text="")

    def _clear_preview(self):
        self._preview_image_path = None
        self._preview_ctk_image = None
        self.preview_label.configure(image=None, text=tr("scan_preview_empty"))

    def _on_photo_double_click(self, event):
        if event.widget is self.photos_list:
            idx = self.photos_list.nearest(event.y)
            if idx < 0 or idx >= self.photos_list.size():
                return
            path = self.photos_list.get(idx)
        else:
            path = self._preview_image_path
        if not path:
            return
        PhotoPreviewCTk(self, path, self.photo_rotations.get(path, 0))

    def _on_toggle_save_copies(self):
        s = self.settings
        s["save_scan_copies"] = self.save_copies_var.get()
        app_config.save_settings(s)

    # -- Run grading -------------------------------------------------------
    def _on_run(self):
        photo_paths = list(self.photos_list.get(0, "end"))
        if not photo_paths:
            messagebox.showwarning(APP_TITLE, tr("scan_need_photo"))
            return
        if not self.roster and not messagebox.askyesno(APP_TITLE, tr("scan_no_csv_confirm")):
            return

        roster_snapshot = dict(self.roster)
        out_of_20 = self.note_out_of_20_var.get()
        save_copies_wanted = self.save_copies_var.get()
        rotations_snapshot = dict(self.photo_rotations)

        classe_name = suggest_run_classe(roster_snapshot)
        suggested_name = build_run_name(roster_snapshot, photo_paths)
        typed_name = simpledialog.askstring(APP_TITLE, tr("scan_ask_run_name"),
                                             initialvalue=suggested_name, parent=self)
        if typed_name is None:
            return
        base_name = _sanitize_filename_part(typed_name.strip()) if typed_name.strip() else suggested_name
        run_name = dedupe_name(base_name, set(class_archive.list_corrections(classe_name)))

        self.run_btn.configure(state="disabled")
        self.progress.start()
        self.status_label.configure(text=tr("scan_reading_photos", n=len(photo_paths)))

        def work():
            run_dir = class_archive.run_dir(classe_name, run_name)
            effective_paths = _materialize_rotations(photo_paths, rotations_snapshot)
            report = process_batch(effective_paths, roster_snapshot, run_dir)
            if self.answer_key:
                scoring.compute_scores(report, self.answer_key)
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
            self.run_btn.configure(state="normal")
            self.status_label.configure(text="")
            if err:
                exc, _tb = err
                messagebox.showerror(APP_TITLE, tr("scan_failed", detail=exc))
                return
            report, run_dir, run_name, classe_name = result
            self.report = report
            self.last_run_dir = run_dir
            self.last_run_name = run_name
            self.last_archive_classe = classe_name
            self.show_results_btn.configure(state="normal")
            self._ensure_results_window()

        run_in_background(self, work, on_done)

    # -- Results window ----------------------------------------------------
    def _ensure_results_window(self):
        if self.results_win is not None and self.results_win.winfo_exists():
            self.results_win.deiconify()
            self.results_win.lift()
            self.results_win.focus_force()
            self._populate_tree()
            self._set_summary(format_batch_report(self.report))
            return

        win = ctk.CTkToplevel(self)
        win.title(tr("res_title"))
        win.geometry("980x700")
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        self.results_win = win

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=10)
        ctk.CTkButton(btn_row, text=tr("res_export_btn"), command=self._on_export).pack(side="left")

        self.summary_text = ctk.CTkTextbox(win, height=110)
        self.summary_text.pack(fill="x", padx=12)

        columns = ("numero", "eleve", "classe", "statut", "lues", "note")
        style = ttk.Style()
        style.configure("Proto.Treeview", rowheight=26, background=LIST_BG, fieldbackground=LIST_BG,
                        borderwidth=0)
        self.tree = ttk.Treeview(win, columns=columns, show="headings", height=16, style="Proto.Treeview")
        for col, label, width in [("numero", tr("col_number"), 60), ("eleve", tr("col_name"), 200),
                                   ("classe", tr("col_class"), 90), ("statut", tr("col_status"), 140),
                                   ("lues", tr("col_read"), 80), ("note", tr("col_note"), 100)]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=12, pady=12)
        self.tree.tag_configure("ok", background="#dff2df")
        self.tree.tag_configure("warn", background="#fdeccb")
        self.tree.tag_configure("unknown", background="#fddede")
        self.tree.tag_configure("missing", background="#e4e9f7")
        self.tree.tag_configure("error", background="#f6c6c6")
        self.tree.tag_configure("read_mismatch", background="#ffcc66")

        self._populate_tree()
        self._set_summary(format_batch_report(self.report))

    def _set_summary(self, text):
        self.summary_text.configure(state="normal")
        self.summary_text.delete("1.0", "end")
        self.summary_text.insert("1.0", text)
        self.summary_text.configure(state="disabled")

    def _populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        tag_map = {"ok": "ok", "feuille_douteuse": "warn", "numero_inconnu": "unknown",
                   "face_manquante": "missing", "erreur_lecture": "error"}
        for idx, entry in enumerate(self.report):
            status = entry.get("status", "erreur_lecture")
            numero = entry.get("sheet_number")
            numero = numero if numero is not None else "-"
            progress = question_read_progress(entry)
            mismatch = progress is not None and progress[0] != progress[1]
            lues_str = f"{progress[0]}/{progress[1]}" if progress is not None else "-"
            tag = "read_mismatch" if mismatch else tag_map.get(status, "error")
            self.tree.insert("", "end", iid=str(idx),
                              values=(numero, entry.get("eleve", ""), entry.get("classe", ""),
                                      status_label(status), lues_str,
                                      format_note(entry, self.note_out_of_20_var.get())),
                              tags=(tag,))

    def _on_export(self):
        if not self.report:
            return
        default_dir = self.last_run_dir or app_config.get_config_dir()
        path = filedialog.asksaveasfilename(title=tr("res_export_title"), initialdir=default_dir,
                                             initialfile=f"{self.last_run_name or 'resultats'}.csv",
                                             defaultextension=".csv", filetypes=[(tr("file_csv"), "*.csv")])
        if not path:
            return
        from qcm_app import write_results_csv
        headers, rows = build_results_csv_rows(self.report, self.note_out_of_20_var.get())
        write_results_csv(path, headers, rows)
        messagebox.showinfo(APP_TITLE, tr("res_exported", path=path))


class PhotoPreviewCTk(ctk.CTkToplevel):
    """CustomTkinter equivalent of qcm_app.PhotoPreviewDialog."""

    def __init__(self, master, path, rotation_degrees=0):
        super().__init__(master)
        self.title(tr("scan_preview_title", name=os.path.basename(path)))
        self._ctk_image = None
        try:
            with Image.open(path) as img:
                img = img.convert("RGB")
                img = _apply_rotation(img, rotation_degrees)
                orig_w, orig_h = img.size
                max_w = int(self.winfo_screenwidth() * 0.9)
                max_h = int(self.winfo_screenheight() * 0.85)
                if orig_w > max_w or orig_h > max_h:
                    ratio = min(max_w / orig_w, max_h / orig_h)
                    img = img.resize((max(1, int(orig_w * ratio)), max(1, int(orig_h * ratio))), Image.LANCZOS)
                self._ctk_image = ctk.CTkImage(light_image=img, size=img.size)
        except (OSError, ValueError) as exc:
            ctk.CTkLabel(self, text=tr("scan_preview_load_error", detail=exc), padx=20, pady=20).pack()
            return
        ctk.CTkLabel(self, image=self._ctk_image, text="").pack()
        self.bind("<Escape>", lambda _e: self.destroy())


def main():
    translations.load_language_from_settings()
    class_archive.migrate_legacy_class_store()
    root = ctk.CTk()
    root.title(f"{APP_TITLE} — prototype CustomTkinter")
    root.geometry("1000x900")

    header = ctk.CTkFrame(root, fg_color="transparent")
    header.pack(fill="x", padx=16, pady=(16, 0))
    ctk.CTkLabel(header, text="QCM Scanner", font=ctk.CTkFont(size=22, weight="bold")).pack(anchor="w")
    ctk.CTkLabel(header, text="Prototype d'interface CustomTkinter — onglet Scanner/Corriger uniquement",
                 text_color="#666666").pack(anchor="w")

    tabs = ctk.CTkTabview(root, width=960)
    tabs.pack(fill="both", expand=True, padx=16, pady=16)
    for name in (tr("tab_data"), tr("tab_generate"), tr("tab_scan"), tr("tab_preferences")):
        tabs.add(name)

    scan_tab_name = tr("tab_scan")
    ScanTabCTk(tabs.tab(scan_tab_name)).pack(fill="both", expand=True)

    for name in (tr("tab_data"), tr("tab_generate"), tr("tab_preferences")):
        placeholder = ctk.CTkFrame(tabs.tab(name), fg_color="transparent")
        placeholder.pack(fill="both", expand=True)
        ctk.CTkLabel(placeholder,
                     text="Cet onglet n'a pas ete reecrit dans ce prototype.\n"
                          "Voir PROTOTYPE_README.md.",
                     text_color="#888888", justify="center").pack(expand=True)

    tabs.set(scan_tab_name)
    root.mainloop()


if __name__ == "__main__":
    main()

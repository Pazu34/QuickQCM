"""
QCM Scanner — application locale (fenêtre) pour générer les feuilles-
réponses et corriger les copies scannées.

Ce fichier est la seule pièce nouvelle qui construit une interface autour
des 4 modules déjà écrits et testés (generate_sheet_modular, detect_modular,
roster_match, sheet_layout_v2) : il ne réimplémente ni le dessin des
feuilles ni la lecture optique, seulement le formulaire, la liste des
résultats et la notation (scoring.py).
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
import class_store

APP_TITLE = "QCM Scanner"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")

SIZE_PRESETS = {
    "Petite (format A6, 4 feuilles par page A4)": dict(scale=1.0, tiles_rows=2, tiles_cols=2),
    "Moyenne (format A5, 2 feuilles par page A4)": dict(scale=2 ** 0.5, tiles_rows=1, tiles_cols=2),
    "Grande (format A4, 1 feuille par page)": dict(scale=2.0, tiles_rows=1, tiles_cols=1),
}

STATUS_LABELS = {
    "ok": "OK",
    "feuille_douteuse": "À vérifier",
    "numero_inconnu": "Numéro inconnu",
    "face_manquante": "Face manquante",
    "erreur_lecture": "Erreur de lecture",
}

HEADER_TOKENS = {"numero", "numéro", "nom", "classe"}


def _parse_tabular_rows(rows, col_index):
    """Construit un roster {numero: {nom, classe}} à partir de lignes
    déjà découpées en colonnes (une ligne = un élève). col_index indique
    l'index de chaque colonne reconnue ('nom' obligatoire, 'numero' et
    'classe' facultatifs). Les élèves sans numéro se voient attribuer le
    plus petit numéro libre, par classe (dans l'ordre où les classes
    apparaissent dans le fichier) puis par ordre alphabétique du nom."""
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
    """Format 'large' : une ligne = une classe, première colonne = nom
    de la classe, colonnes suivantes = noms des élèves de cette classe
    (ex. classe1;Alice;Bob puis classe2;Chloé;David;Eve sur la ligne
    suivante). Les numéros sont attribués séquentiellement, classe par
    classe (dans l'ordre des lignes), par ordre alphabétique du nom à
    l'intérieur de chaque classe."""
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
    """Charge un CSV élèves de façon tolérante. Essaie d'abord le format
    standard numero;nom;classe (roster_match.load_roster, déjà testé) ;
    si la colonne 'numero' est absente ou incomplète, détecte
    automatiquement soit un format nom[;classe] (numéros attribués
    automatiquement), soit le format 'large' une ligne par classe.
    Retourne (roster, message_ou_None) — message n'est pas None si les
    numéros ont été attribués automatiquement (l'appelant doit alors
    proposer d'enregistrer un CSV au format standard)."""
    try:
        roster = load_roster(path)
        if roster:
            return roster, None
    except (KeyError, ValueError):
        pass

    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [row for row in csv.reader(f, delimiter=";") if any(c.strip() for c in row)]
    if not rows:
        raise ValueError("Le fichier CSV est vide.")

    header = [c.strip().lower() for c in rows[0]]
    looks_like_header = len(header) <= 3 and any(tok in HEADER_TOKENS for tok in header)

    auto_msg = "Numéros attribués automatiquement (par classe, puis par ordre alphabétique du nom)."

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
            auto_msg = ("Fichier multi-classes détecté (une ligne = une classe) : numéros attribués "
                        "automatiquement, classe par classe puis par ordre alphabétique du nom.")

    if not roster:
        raise ValueError(
            "Impossible de comprendre ce fichier CSV. Formats reconnus : « numero;nom;classe », "
            "« nom;classe » (sans numéro), ou une ligne par classe « classe;élève1;élève2;... »."
        )
    return roster, auto_msg


def export_roster_csv(path, roster):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["numero", "nom", "classe"])
        for num in sorted(roster.keys()):
            w.writerow([num, roster[num]["nom"], roster[num].get("classe", "")])


def smart_load_roster_with_export(path):
    """smart_load_roster + enregistrement automatique d'un CSV au format
    standard si des numéros ont été attribués automatiquement. Retourne
    (roster, chemin_a_utiliser_ensuite, message_ou_None)."""
    roster, message = smart_load_roster(path)
    if message:
        base, _ext = os.path.splitext(path)
        export_path = base + "_numerote.csv"
        export_roster_csv(export_path, roster)
        message += f"\n\nFichier au format standard enregistré :\n{export_path}\n\nUtilise ce fichier (et non " \
                    "l'original) pour la correction des copies : il contient les numéros attribués aux élèves."
        return roster, export_path, message
    return roster, path, None


def suggest_class_name(roster):
    """Devine un nom de classe par défaut pour 'Enregistrer en mémoire' :
    la valeur de 'classe' la plus fréquente dans le roster, s'il y en a
    une."""
    from collections import Counter
    classes = [info.get("classe", "").strip() for info in roster.values() if info.get("classe", "").strip()]
    if classes:
        return Counter(classes).most_common(1)[0][0]
    return ""


# ---------------------------------------------------------------------
# Génération de feuilles pour une liste de numéros ARBITRAIRE (pas
# forcément consécutive) — cas d'un CSV classe déjà existant où chaque
# élève a déjà un numéro attribué. Reprend exactement la même boucle de
# pavage que build_batch (generate_sheet_modular.py), seul le choix des
# numéros change ; le dessin lui-même (draw_sheet) n'est pas touché.
# ---------------------------------------------------------------------
def build_for_numbers(path, cfg, numbers, identities=None):
    """identities : {numero (int): (nom, classe)} optionnel — cf.
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
    """Exécute `work()` dans un thread séparé (pour ne pas geler la
    fenêtre) puis rappelle `on_done(error, result)` sur le thread
    principal Tkinter via `.after()`."""
    def runner():
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - on veut tout attraper pour l'afficher
            err = (exc, traceback.format_exc())
            widget.after(0, lambda: on_done(err, None))
        else:
            widget.after(0, lambda: on_done(None, result))
    threading.Thread(target=runner, daemon=True).start()


def offer_csv_template(parent):
    """Crée un modèle de CSV classe (colonnes numero,nom,classe) prêt à
    être complété dans Excel/LibreOffice, puis l'ouvre. Le numéro de
    chaque ligne doit correspondre au numéro imprimé sur la feuille-
    réponse de l'élève correspondant."""
    n = simpledialog.askinteger(APP_TITLE, "Combien d'élèves (lignes) dans le modèle ?",
                                 initialvalue=30, minvalue=1, maxvalue=255, parent=parent)
    if not n:
        return None
    path = filedialog.asksaveasfilename(
        title="Enregistrer le modèle de CSV classe", defaultextension=".csv",
        initialfile="modele_classe.csv", filetypes=[("Fichier CSV", "*.csv")], parent=parent)
    if not path:
        return None
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["numero", "nom", "classe"])
        for i in range(1, n + 1):
            w.writerow([i, "", ""])
    messagebox.showinfo(APP_TITLE, f"Modèle créé :\n{path}\n\n"
                                    "Complète la colonne « nom » (et « classe » si besoin) dans Excel ou "
                                    "LibreOffice, enregistre le fichier, puis charge-le avec « Parcourir… ».\n\n"
                                    "Le numéro de chaque ligne doit correspondre au numéro imprimé sur la "
                                    "feuille-réponse de cet élève.")
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
    """Cellule d'en-tête (lettre A/B/C...) de largeur FIXE, pour que les
    lettres restent alignées avec les cases à cocher en dessous (un
    Label et un Checkbutton de même 'width' ne font pas la même largeur
    réelle à l'écran)."""
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
    """Cadre avec ascenseur vertical, utilisé pour la grille du corrigé
    (jusqu'à 64 questions) et pour le détail d'une copie."""

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
# Onglet 1 : génération des feuilles-réponses
# ---------------------------------------------------------------------
class GenerateTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=12)
        self.settings = app_config.load_settings()
        self.roster_mode = tk.StringVar(value="manuel")
        self.csv_path = tk.StringVar(value="")
        self.loaded_csv_roster = None  # list de (numero, nom, classe)
        self._build_ui()

    def _build_ui(self):
        s = self.settings

        form = ttk.LabelFrame(self, text="Feuille-réponse", padding=10)
        form.pack(fill="x", pady=(0, 10))
        form.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(form, text="Titre :").grid(row=row, column=0, sticky="w", pady=3)
        self.title_var = tk.StringVar(value=s.get("title", "Contrôle"))
        ttk.Entry(form, textvariable=self.title_var, width=40).grid(row=row, column=1, sticky="we", padx=5)

        row += 1
        ttk.Label(form, text="Sous-titre :").grid(row=row, column=0, sticky="w", pady=3)
        self.subtitle_var = tk.StringVar(value=s.get("subtitle", "QCM Physique-Chimie"))
        ttk.Entry(form, textvariable=self.subtitle_var, width=40).grid(row=row, column=1, sticky="we", padx=5)

        row += 1
        ttk.Label(form, text="Nombre de questions (1 à 64) :").grid(row=row, column=0, sticky="w", pady=3)
        self.n_questions_var = tk.IntVar(value=s.get("n_questions", 12))
        ttk.Spinbox(form, from_=1, to=MAX_QUESTIONS, textvariable=self.n_questions_var, width=8).grid(
            row=row, column=1, sticky="w", padx=5)

        row += 1
        ttk.Label(form, text="Nombre de réponses par question (3 à 6) :").grid(row=row, column=0, sticky="w", pady=3)
        self.n_choices_var = tk.IntVar(value=s.get("n_choices", 5))
        ttk.Spinbox(form, from_=3, to=6, textvariable=self.n_choices_var, width=8).grid(
            row=row, column=1, sticky="w", padx=5)

        row += 1
        self.show_classe_var = tk.BooleanVar(value=s.get("show_classe", True))
        ttk.Checkbutton(form, text="Afficher un champ « Classe »", variable=self.show_classe_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=3)

        row += 1
        ttk.Label(form, text="Taille de la feuille :").grid(row=row, column=0, sticky="w", pady=3)
        self.preset_var = tk.StringVar(value=s.get("preset", list(SIZE_PRESETS.keys())[0]))
        ttk.Combobox(form, textvariable=self.preset_var, values=list(SIZE_PRESETS.keys()),
                     state="readonly", width=38).grid(row=row, column=1, sticky="we", padx=5)

        roster_frame = ttk.LabelFrame(self, text="Élèves", padding=10)
        roster_frame.pack(fill="x", pady=(0, 10))

        modes = [
            ("manuel", "Saisir la liste des noms maintenant"),
            ("csv", "J'ai déjà un fichier CSV (numero,nom,classe)"),
            ("aucun", "Pas de liste pour l'instant (juste des numéros)"),
        ]
        for value, label in modes:
            mode_row = ttk.Frame(roster_frame)
            mode_row.pack(fill="x", anchor="w")
            ttk.Radiobutton(mode_row, text=label, variable=self.roster_mode, value=value,
                            command=self._on_roster_mode_change).pack(side="left", anchor="w")
            if value == "csv":
                ttk.Button(mode_row, text="Créer un modèle…", command=lambda: offer_csv_template(self)).pack(
                    side="left", padx=10)

        self.manual_frame = ttk.Frame(roster_frame)
        ttk.Label(self.manual_frame, text="Un nom par ligne (l'ordre donne le numéro de feuille) :").pack(anchor="w")
        self.names_text = tk.Text(self.manual_frame, height=8, width=45)
        self.names_text.pack(fill="x", pady=3)
        classe_row = ttk.Frame(self.manual_frame)
        classe_row.pack(fill="x")
        ttk.Label(classe_row, text="Classe (facultatif, appliquée à tous) :").pack(side="left")
        self.manual_classe_var = tk.StringVar(value=s.get("last_classe", ""))
        ttk.Entry(classe_row, textvariable=self.manual_classe_var, width=15).pack(side="left", padx=5)

        self.csv_frame = ttk.Frame(roster_frame)
        csv_row = ttk.Frame(self.csv_frame)
        csv_row.pack(fill="x", pady=3)
        ttk.Entry(csv_row, textvariable=self.csv_path, width=40).pack(side="left", fill="x", expand=True)
        ttk.Button(csv_row, text="Parcourir…", command=self._browse_csv).pack(side="left", padx=5)
        csv_row2 = ttk.Frame(self.csv_frame)
        csv_row2.pack(fill="x", pady=(0, 3))
        ttk.Button(csv_row2, text="Tableau élèves (voir/modifier/mémoire)…",
                   command=self._open_roster_manager).pack(side="left")
        ttk.Button(csv_row2, text="Enregistrer cette classe en mémoire",
                   command=self._save_class_to_memory).pack(side="left", padx=5)
        self.csv_info_label = ttk.Label(self.csv_frame, text="", foreground="#555555")
        self.csv_info_label.pack(anchor="w")

        self.aucun_frame = ttk.Frame(roster_frame)
        n_row = ttk.Frame(self.aucun_frame)
        n_row.pack(fill="x", pady=3)
        ttk.Label(n_row, text="Nombre de feuilles à générer :").pack(side="left")
        self.n_sheets_var = tk.IntVar(value=s.get("n_sheets", 30))
        ttk.Spinbox(n_row, from_=1, to=255, textvariable=self.n_sheets_var, width=8).pack(side="left", padx=5)
        start_row = ttk.Frame(self.aucun_frame)
        start_row.pack(fill="x", pady=3)
        ttk.Label(start_row, text="Numéro de départ :").pack(side="left")
        self.start_number_var = tk.IntVar(value=s.get("start_number", 1))
        ttk.Spinbox(start_row, from_=1, to=255, textvariable=self.start_number_var, width=8).pack(side="left", padx=5)

        identity_frame = ttk.Frame(roster_frame)
        identity_frame.pack(fill="x", pady=(8, 0), anchor="w")
        self.print_name_var = tk.BooleanVar(value=s.get("print_name", False))
        ttk.Checkbutton(identity_frame, text="Écrire le nom des élèves directement sur la fiche",
                        variable=self.print_name_var).pack(anchor="w")
        self.print_classe_var = tk.BooleanVar(value=s.get("print_classe", False))
        ttk.Checkbutton(identity_frame, text="Écrire la classe des élèves directement sur la fiche",
                        variable=self.print_classe_var).pack(anchor="w")
        ttk.Label(identity_frame, text="(sinon, une ligne vierge est imprimée pour que l'élève l'écrive à la main)",
                  foreground="#777777").pack(anchor="w")

        self.extra_blank_frame = ttk.Frame(identity_frame)
        self.extra_blank_enabled_var = tk.BooleanVar(value=s.get("extra_blank_enabled", False))
        ttk.Checkbutton(self.extra_blank_frame, text="Générer", variable=self.extra_blank_enabled_var).pack(
            side="left")
        self.extra_blank_n_var = tk.IntVar(value=s.get("extra_blank_n", 3))
        ttk.Spinbox(self.extra_blank_frame, from_=1, to=10, textvariable=self.extra_blank_n_var, width=4).pack(
            side="left", padx=5)
        ttk.Label(self.extra_blank_frame,
                  text="fiche(s) supplémentaire(s) sans nom ni classe pré-remplis (absents, retirages…)").pack(
            side="left")
        self.print_name_var.trace_add("write", self._update_extra_blank_visibility)
        self.print_classe_var.trace_add("write", self._update_extra_blank_visibility)
        self._update_extra_blank_visibility()

        self._on_roster_mode_change()

        out_frame = ttk.LabelFrame(self, text="Dossier de sortie", padding=10)
        out_frame.pack(fill="x", pady=(0, 10))
        self.output_dir_var = tk.StringVar(
            value=s.get("generate_output_dir", os.path.join(app_config.default_documents_dir(), "Feuilles générées")))
        row2 = ttk.Frame(out_frame)
        row2.pack(fill="x")
        ttk.Entry(row2, textvariable=self.output_dir_var, width=50).pack(side="left", fill="x", expand=True)
        ttk.Button(row2, text="Parcourir…", command=self._browse_output_dir).pack(side="left", padx=5)

        action_row = ttk.Frame(self)
        action_row.pack(fill="x", pady=6)
        self.preview_btn = ttk.Button(action_row, text="Générer un aperçu", command=self._on_preview)
        self.preview_btn.pack(side="left")
        self.generate_btn = ttk.Button(action_row, text="Générer le PDF", command=self._on_generate)
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
        path = filedialog.askopenfilename(title="Choisir le fichier CSV de la classe",
                                           filetypes=[("Fichiers CSV", "*.csv"), ("Tous les fichiers", "*.*")])
        if not path:
            return
        try:
            roster, use_path, message = smart_load_roster_with_export(path)
        except (OSError, KeyError, ValueError) as exc:
            messagebox.showerror(APP_TITLE, f"Impossible de lire ce fichier CSV.\n\nDétail : {exc}")
            return
        if message:
            messagebox.showinfo(APP_TITLE, message)
        self.csv_path.set(use_path)
        self.loaded_csv_roster = roster
        self.csv_info_label.config(text=f"{len(roster)} élève(s) chargé(s), numéros {min(roster)} à {max(roster)}.")

    def _open_roster_manager(self):
        def on_apply(roster, name):
            self.loaded_csv_roster = roster
            self.csv_path.set(f"(classe en mémoire : {name})" if name else "(tableau modifié manuellement)")
            self.csv_info_label.config(
                text=f"{len(roster)} élève(s) chargé(s), numéros {min(roster)} à {max(roster)}.")
            self.roster_mode.set("csv")
            self._on_roster_mode_change()

        RosterManagerDialog(self, initial_roster=self.loaded_csv_roster,
                             initial_name=suggest_class_name(self.loaded_csv_roster) if self.loaded_csv_roster
                             else None,
                             on_apply=on_apply)

    def _save_class_to_memory(self):
        roster = self.loaded_csv_roster
        if not roster:
            messagebox.showwarning(APP_TITLE, "Charge d'abord une liste d'élèves (CSV) avant de l'enregistrer "
                                                "en mémoire.")
            return
        suggested = suggest_class_name(roster)
        name = simpledialog.askstring(APP_TITLE, "Nom de la classe à enregistrer en mémoire :",
                                       initialvalue=suggested, parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in class_store.list_classes():
            if not messagebox.askyesno(APP_TITLE, f"Une classe « {name} » existe déjà en mémoire. La remplacer ?"):
                return
        class_store.save_class(name, roster)
        messagebox.showinfo(APP_TITLE, f"Classe « {name} » enregistrée en mémoire ({len(roster)} élève(s)).")

    def _browse_output_dir(self):
        d = filedialog.askdirectory(title="Choisir le dossier de sortie")
        if d:
            self.output_dir_var.set(d)

    def _build_config(self):
        preset = SIZE_PRESETS[self.preset_var.get()]
        return SheetConfig(
            title=self.title_var.get().strip() or "Contrôle",
            subtitle=self.subtitle_var.get().strip(),
            n_questions=int(self.n_questions_var.get()),
            n_choices=int(self.n_choices_var.get()),
            show_classe=bool(self.show_classe_var.get()),
            scale=preset["scale"],
            tiles_rows=preset["tiles_rows"],
            tiles_cols=preset["tiles_cols"],
        )

    def _save_settings(self):
        s = self.settings
        s.update(dict(
            title=self.title_var.get(), subtitle=self.subtitle_var.get(),
            n_questions=int(self.n_questions_var.get()), n_choices=int(self.n_choices_var.get()),
            show_classe=bool(self.show_classe_var.get()), preset=self.preset_var.get(),
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
            messagebox.showerror(APP_TITLE, f"Configuration invalide :\n\n{exc}")
            return

        n_tiles = cfg.tiles_rows * cfg.tiles_cols
        fd, preview_path = tempfile.mkstemp(prefix="qcm_apercu_", suffix=".pdf")
        os.close(fd)
        try:
            build(preview_path, cfg, numbers=list(range(1, n_tiles + 1)))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Impossible de générer l'aperçu :\n\n{exc}")
            return
        os.startfile(preview_path)

    def _on_generate(self):
        try:
            cfg = self._build_config()
        except (tk.TclError, ValueError) as exc:
            messagebox.showerror(APP_TITLE, f"Configuration invalide : {exc}")
            return

        mode = self.roster_mode.get()
        names, classe_for_all, numbers_from_csv, roster_rows = None, "", None, None
        if mode == "manuel":
            raw = self.names_text.get("1.0", "end").splitlines()
            names = [n.strip() for n in raw if n.strip()]
            if not names:
                messagebox.showwarning(APP_TITLE, "Merci de saisir au moins un nom, ou choisis une autre option "
                                                    "pour la liste d'élèves.")
                return
            classe_for_all = self.manual_classe_var.get().strip()
        elif mode == "csv":
            if not self.loaded_csv_roster:
                messagebox.showwarning(APP_TITLE, "Merci de choisir un fichier CSV valide.")
                return
            roster_rows = self.loaded_csv_roster
            numbers_from_csv = sorted(roster_rows.keys())

        output_dir = self.output_dir_var.get().strip()
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Impossible d'utiliser ce dossier de sortie : {exc}")
            return

        self.generate_btn.config(state="disabled")
        self.progress.start(12)
        self.status_label.config(text="Génération en cours…")

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
                messagebox.showerror(APP_TITLE, f"Échec de la génération :\n\n{exc}")
                self.status_label.config(text="")
                return
            pdf_path, csv_path, generated_numbers, used_cfg, n_extra = result
            self._save_settings()
            rv = " (recto-verso automatique)" if needs_recto_verso(used_cfg) else ""
            msg = (f"{len(generated_numbers)} feuille(s) générée(s), numéros "
                   f"{min(generated_numbers)} à {max(generated_numbers)}{rv}.\nPDF : {pdf_path}")
            if n_extra:
                extra_start = max(generated_numbers) - n_extra + 1
                msg += (f"\nDont {n_extra} fiche(s) vierge(s) supplémentaire(s) sans nom ni classe pré-remplis "
                        f"(numéros {extra_start} à {max(generated_numbers)}).")
            if csv_path:
                msg += f"\nCSV classe : {csv_path}"
            self.status_label.config(text=msg)
            if messagebox.askyesno(APP_TITLE, msg + "\n\nOuvrir le dossier maintenant ?"):
                os.startfile(output_dir)

        run_in_background(self, work, on_done)


# ---------------------------------------------------------------------
# Fenêtre de saisie du corrigé (facultatif)
# ---------------------------------------------------------------------
class AnswerKeyDialog(tk.Toplevel):
    def __init__(self, master, n_questions, n_choices, existing_key=None):
        super().__init__(master)
        self.title("Corrigé — sélectionner la ou les bonnes réponses")
        self.geometry("560x600")
        self.result = None
        self._initial_key = existing_key or {}
        self.letters = []
        self.vars = {}

        ttk.Label(self, text="Coche la ou les bonnes réponses pour chaque question.\n"
                              "Laisse une question sans coche pour l'exclure de la notation.",
                  justify="left", wraplength=520).pack(fill="x", padx=10, pady=8)

        top_row = ttk.Frame(self)
        top_row.pack(fill="x", padx=10)
        ttk.Label(top_row, text="Nombre de questions :").pack(side="left")
        self.n_q_var = tk.IntVar(value=n_questions)
        ttk.Spinbox(top_row, from_=1, to=MAX_QUESTIONS, textvariable=self.n_q_var, width=6,
                    command=self._rebuild_grid).pack(side="left", padx=5)
        ttk.Label(top_row, text="Nombre de réponses par question :").pack(side="left", padx=(15, 0))
        self.n_choices_var = tk.IntVar(value=n_choices)
        ttk.Spinbox(top_row, from_=3, to=6, textvariable=self.n_choices_var, width=6,
                    command=self._rebuild_grid).pack(side="left", padx=5)

        self.scroll = ScrollableFrame(self, height=420)
        self.scroll.pack(fill="both", expand=True, padx=10, pady=8)

        self._rebuild_grid()

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=10, pady=8)
        ttk.Button(btn_row, text="Tout effacer", command=self._clear_all).pack(side="left")
        ttk.Button(btn_row, text="Annuler", command=self.destroy).pack(side="right")
        ttk.Button(btn_row, text="Valider", command=self._on_validate).pack(side="right", padx=5)

        self.transient(master)
        self.grab_set()

    def _clear_all(self):
        for v in self.vars.values():
            v.set(False)

    def _current_key(self):
        key = {}
        for (q, letter), v in self.vars.items():
            if v.get():
                key.setdefault(q, []).append(letter)
        return key

    def _rebuild_grid(self):
        # Préserve les cases déjà cochées quand on change le nombre de
        # questions ou de réponses (sinon changer l'un efface l'autre).
        prev_key = self._current_key() if self.vars else self._initial_key
        for child in self.scroll.inner.winfo_children():
            child.destroy()
        self.vars = {}
        n_q = int(self.n_q_var.get())
        n_c = int(self.n_choices_var.get())
        self.letters = ["A", "B", "C", "D", "E", "F"][:n_c]
        header = ttk.Frame(self.scroll.inner)
        header.pack(fill="x")
        ttk.Label(header, text="Question", width=10).pack(side="left")
        for letter in self.letters:
            grid_header_cell(header, letter)
        for q in range(1, n_q + 1):
            row = ttk.Frame(self.scroll.inner)
            row.pack(fill="x")
            ttk.Label(row, text=f"Q{q}", width=10).pack(side="left")
            for letter in self.letters:
                v = tk.BooleanVar(value=letter in prev_key.get(q, []))
                self.vars[(q, letter)] = v
                grid_check_cell(row, v)

    def _on_validate(self):
        key = {}
        n_q = int(self.n_q_var.get())
        for q in range(1, n_q + 1):
            letters = [letter for letter in self.letters if self.vars[(q, letter)].get()]
            if letters:
                key[q] = letters
        if not key:
            if not messagebox.askyesno(APP_TITLE, "Aucune bonne réponse cochée : la notation sera désactivée. "
                                                    "Continuer ?"):
                return
            self.result = {}
        else:
            self.result = key
        self.destroy()


# ---------------------------------------------------------------------
# Fenêtre de détail d'une copie (association manuelle, correction des
# réponses détectées, visualisation de la zone Nom/Classe recadrée).
# ---------------------------------------------------------------------
class DetailDialog(tk.Toplevel):
    def __init__(self, master, entry, roster, on_save):
        super().__init__(master)
        self.entry = entry
        self.roster = roster or {}
        self.on_save = on_save
        num = entry.get("sheet_number")
        num_label = f"n°{num}" if num is not None else "(numéro non renseigné)"
        self.title(f"Feuille {num_label}")
        self.geometry("640x680")

        status = entry.get("status")
        header = ttk.Frame(self, padding=10)
        header.pack(fill="x")
        ttk.Label(header, text=f"Feuille {num_label} — {STATUS_LABELS.get(status, status)}",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        if status == "face_manquante":
            faces = ", ".join("recto" if s == 0 else "verso" for s in entry.get("missing_sides", []))
            ttk.Label(header, text=f"Face(s) manquante(s) : {faces}. Rescanne la face manquante puis "
                                    "relance la correction — cette feuille ne peut pas être corrigée ici.",
                      wraplength=580, foreground="#a05a00", justify="left").pack(anchor="w", pady=4)
            expected = self.roster.get(num)
            if expected:
                ttk.Label(header, text=f"Élève attendu (d'après le CSV) : {expected['nom']}").pack(anchor="w")
            ttk.Button(self, text="Fermer", command=self.destroy).pack(pady=10)
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
            ttk.Label(img_frame, text="(zone Nom/Classe non disponible pour cette feuille)",
                      foreground="#777777").pack(anchor="w")
        sources = entry.get("sources") or []
        if sources:
            ttk.Button(img_frame, text="Ouvrir la photo d'origine",
                       command=lambda: os.startfile(sources[0])).pack(anchor="w", pady=(0, 6))

        identity_frame = ttk.Frame(self, padding=10)
        identity_frame.pack(fill="x")
        ttk.Label(identity_frame, text="Élève :").grid(row=0, column=0, sticky="w")
        self.nom_var = tk.StringVar(value=entry.get("eleve", ""))
        ttk.Entry(identity_frame, textvariable=self.nom_var, width=30).grid(row=0, column=1, sticky="w", padx=5)
        ttk.Label(identity_frame, text="Classe :").grid(row=0, column=2, sticky="w", padx=(15, 0))
        self.classe_var = tk.StringVar(value=entry.get("classe", ""))
        ttk.Entry(identity_frame, textvariable=self.classe_var, width=12).grid(row=0, column=3, sticky="w", padx=5)

        ttk.Label(self, text="Réponses détectées (modifiables ; les lignes orange sont incertaines) :",
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
                ttk.Label(row, text=f"⚠ à vérifier : {', '.join(qres['flagged'])}",
                          foreground="#a05a00").pack(side="left", padx=5)

        btn_row = ttk.Frame(self, padding=10)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Annuler", command=self.destroy).pack(side="right")
        ttk.Button(btn_row, text="Enregistrer", command=self._on_save_click).pack(side="right", padx=5)

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
# Fenêtre de saisie manuelle pour une photo en échec de lecture (repères
# abîmés/déchirés, feuille trop mal cadrée...). La détection automatique
# est impossible sans les repères, donc l'enseignant lit lui-même la
# copie (avec une simple rotation d'affichage pour la remettre à
# l'endroit) et saisit numéro, élève et réponses à la main.
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
        self.title("Saisie manuelle — échec de lecture automatique")

        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        win_w = min(1180, int(screen_w * 0.92))
        win_h = min(840, int(screen_h * 0.88))
        self.geometry(f"{win_w}x{win_h}")
        self._img_max_w = int(win_w * 0.45)
        self._img_max_h = win_h - 190

        ttk.Label(self, text=f"Échec de la lecture automatique : {entry.get('erreur', '(raison inconnue)')}\n"
                              "Regarde la photo ci-contre (pivote-la si besoin) et saisis toi-même le numéro "
                              "et les réponses.",
                  foreground="#a03030", wraplength=win_w - 20, justify="left", padding=10).pack(fill="x")

        # Boutons épinglés en bas AVANT le contenu principal : ils restent
        # toujours visibles même si le contenu au-dessus est très grand.
        btn_row = ttk.Frame(self, padding=10)
        btn_row.pack(side="bottom", fill="x")
        ttk.Button(btn_row, text="Annuler", command=self.destroy).pack(side="right")
        ttk.Button(btn_row, text="Enregistrer les informations saisies", command=self._on_save_click).pack(
            side="right", padx=5)

        main_frame = ttk.Frame(self)
        main_frame.pack(fill="both", expand=True, padx=10)

        # --- Colonne de gauche : la photo ---
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
        ttk.Button(btn_row_img, text="Pivoter 90°", command=self._rotate).pack(side="left")
        if self.source:
            ttk.Button(btn_row_img, text="Ouvrir la photo d'origine",
                       command=lambda: os.startfile(self.source)).pack(side="left", padx=5)

        # --- Colonne de droite : identité + réponses ---
        right = ttk.Frame(main_frame)
        right.pack(side="left", fill="both", expand=True, padx=(15, 0))

        identity_frame = ttk.Frame(right)
        identity_frame.pack(fill="x", pady=(0, 4))
        ttk.Label(identity_frame, text="Numéro de la feuille (si lisible) :").grid(
            row=0, column=0, sticky="w", pady=2)
        self.numero_var = tk.StringVar(value="")
        ttk.Entry(identity_frame, textvariable=self.numero_var, width=8).grid(row=0, column=1, sticky="w", padx=5)
        ttk.Button(identity_frame, text="Saisir automatiquement le nom",
                   command=self._on_autofill_name_click).grid(row=0, column=2, sticky="w", padx=5)
        ttk.Label(identity_frame, text="Élève :").grid(row=1, column=0, sticky="w", pady=2)
        self.nom_var = tk.StringVar(value="")
        ttk.Entry(identity_frame, textvariable=self.nom_var, width=28).grid(row=1, column=1, sticky="w", padx=5)
        ttk.Label(identity_frame, text="Classe :").grid(row=2, column=0, sticky="w", pady=2)
        self.classe_var = tk.StringVar(value="")
        ttk.Entry(identity_frame, textvariable=self.classe_var, width=12).grid(row=2, column=1, sticky="w", padx=5)

        grid_row = ttk.Frame(right)
        grid_row.pack(fill="x", pady=(4, 4))
        ttk.Label(grid_row, text="Nombre de questions :").pack(side="left")
        self.n_q_var = tk.IntVar(value=default_n_questions)
        ttk.Spinbox(grid_row, from_=1, to=MAX_QUESTIONS, textvariable=self.n_q_var, width=6,
                    command=self._rebuild_grid).pack(side="left", padx=5)
        ttk.Label(grid_row, text="Nombre de réponses :").pack(side="left", padx=(15, 0))
        self.n_c_var = tk.IntVar(value=default_n_choices)
        ttk.Spinbox(grid_row, from_=3, to=6, textvariable=self.n_c_var, width=6,
                    command=self._rebuild_grid).pack(side="left", padx=5)

        ttk.Label(right, text="Coche la ou les réponses cochées par l'élève, pour chaque question :").pack(
            anchor="w", pady=(4, 2))
        self.scroll = ScrollableFrame(right, height=max(200, win_h - 340))
        self.scroll.pack(fill="both", expand=True)
        self._rebuild_grid()

        self.transient(master)
        self.grab_set()

    def _on_autofill_name_click(self):
        raw = self.numero_var.get().strip()
        if not raw.isdigit():
            messagebox.showwarning(APP_TITLE, "Saisis d'abord un numéro de feuille valide.")
            return
        num = int(raw)
        student = self.roster.get(num)
        if not student:
            messagebox.showinfo(APP_TITLE, f"Aucun élève avec le numéro {num} dans le tableau chargé.")
            return
        self.nom_var.set(student["nom"])
        self.classe_var.set(student.get("classe", ""))

    def _refresh_image(self):
        if self._raw_img is None:
            self._img_label.configure(text="(photo introuvable sur le disque)", image="")
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
        ttk.Label(header, text="Question", width=10).pack(side="left")
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
                messagebox.showwarning(APP_TITLE, "Le numéro de feuille doit être un nombre entier.")
                return
        if numero is None:
            if not messagebox.askyesno(APP_TITLE, "Aucun numéro de feuille saisi : la copie sera enregistrée sans "
                                                    "numéro. Continuer ?"):
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


# ---------------------------------------------------------------------
# Fenêtre unique pour voir/modifier un tableau numero/nom/classe, et
# gérer les classes enregistrées en mémoire (class_store.py) : charger,
# enregistrer, renommer, supprimer. Utilisée depuis les deux onglets.
# ---------------------------------------------------------------------
class RosterManagerDialog(tk.Toplevel):
    def __init__(self, master, initial_roster=None, initial_name=None, on_apply=None):
        super().__init__(master)
        self.on_apply = on_apply
        self.rows = []
        self.title("Tableau élèves — voir, modifier, mémoriser")
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        self.geometry(f"{min(700, int(screen_w * 0.7))}x{min(800, int(screen_h * 0.85))}")

        store_frame = ttk.LabelFrame(self, text="Classes enregistrées en mémoire", padding=10)
        store_frame.pack(fill="x", padx=10, pady=(10, 6))
        row1 = ttk.Frame(store_frame)
        row1.pack(fill="x")
        ttk.Label(row1, text="Classe :").pack(side="left")
        self.store_combo = ttk.Combobox(row1, state="readonly", width=25)
        self.store_combo.pack(side="left", padx=5)
        ttk.Button(row1, text="Charger", command=self._on_load_from_store).pack(side="left", padx=3)
        ttk.Button(row1, text="Renommer…", command=self._on_rename_in_store).pack(side="left", padx=3)
        ttk.Button(row1, text="Supprimer", command=self._on_delete_from_store).pack(side="left", padx=3)
        self._refresh_store_combo(select=initial_name)

        save_row = ttk.Frame(store_frame)
        save_row.pack(fill="x", pady=(8, 0))
        ttk.Label(save_row, text="Enregistrer le tableau ci-dessous sous le nom :").pack(side="left")
        self.save_name_var = tk.StringVar(value=initial_name or "")
        ttk.Entry(save_row, textvariable=self.save_name_var, width=20).pack(side="left", padx=5)
        ttk.Button(save_row, text="Enregistrer en mémoire", command=self._on_save_to_store).pack(side="left", padx=3)

        table_frame = ttk.LabelFrame(self, text="Tableau numéro / nom / classe", padding=10)
        table_frame.pack(fill="both", expand=True, padx=10, pady=6)
        header = ttk.Frame(table_frame)
        header.pack(fill="x")
        ttk.Label(header, text="Numéro", width=8).pack(side="left", padx=2)
        ttk.Label(header, text="Nom", width=28).pack(side="left", padx=2)
        ttk.Label(header, text="Classe", width=12).pack(side="left", padx=2)
        self.scroll = ScrollableFrame(table_frame, height=380)
        self.scroll.pack(fill="both", expand=True, pady=(4, 0))
        ttk.Button(table_frame, text="+ Ajouter une ligne", command=lambda: self._add_row()).pack(
            anchor="w", pady=6)

        if initial_roster:
            self._load_roster_into_table(initial_roster)

        action_row = ttk.Frame(self, padding=10)
        action_row.pack(fill="x")
        ttk.Button(action_row, text="Annuler", command=self.destroy).pack(side="right")
        if on_apply:
            ttk.Button(action_row, text="Utiliser ce tableau", command=self._on_use_click).pack(
                side="right", padx=5)

        self.transient(master)
        self.grab_set()

    # -- Lignes du tableau -------------------------------------------------
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
                errors.append(f"Numéro invalide pour « {nom or '(sans nom)'} ».")
                continue
            num = int(numero_str)
            if num in seen:
                errors.append(f"Le numéro {num} est utilisé plusieurs fois.")
                continue
            if not nom:
                errors.append(f"Nom manquant pour le numéro {num}.")
                continue
            seen.add(num)
            roster[num] = {"nom": nom, "classe": classe}
        return roster, errors

    # -- Classes enregistrées -----------------------------------------------
    def _refresh_store_combo(self, select=None):
        names = class_store.list_classes()
        self.store_combo.configure(values=names)
        if select and select in names:
            self.store_combo.set(select)
        elif names and not self.store_combo.get():
            pass

    def _on_load_from_store(self):
        name = self.store_combo.get()
        if not name:
            messagebox.showinfo(APP_TITLE, "Choisis d'abord une classe dans la liste.")
            return
        if self.rows and not messagebox.askyesno(
                APP_TITLE, "Le tableau actuel sera remplacé par la classe chargée. Continuer ?"):
            return
        roster = class_store.load_class(name)
        self._load_roster_into_table(roster)
        self.save_name_var.set(name)

    def _on_rename_in_store(self):
        name = self.store_combo.get()
        if not name:
            messagebox.showinfo(APP_TITLE, "Choisis d'abord une classe dans la liste.")
            return
        new_name = simpledialog.askstring(APP_TITLE, f"Nouveau nom pour « {name} » :", initialvalue=name,
                                           parent=self)
        if not new_name or new_name.strip() == name:
            return
        class_store.rename_class(name, new_name.strip())
        self._refresh_store_combo(select=new_name.strip())
        if self.save_name_var.get() == name:
            self.save_name_var.set(new_name.strip())

    def _on_delete_from_store(self):
        name = self.store_combo.get()
        if not name:
            messagebox.showinfo(APP_TITLE, "Choisis d'abord une classe dans la liste.")
            return
        if messagebox.askyesno(APP_TITLE, f"Supprimer définitivement la classe « {name} » de la mémoire ?\n"
                                           "(le tableau affiché ici n'est pas effacé)"):
            class_store.delete_class(name)
            self._refresh_store_combo()

    def _on_save_to_store(self):
        roster, errors = self._extract_roster()
        if errors:
            messagebox.showerror(APP_TITLE, "Corrige d'abord :\n\n" + "\n".join(errors))
            return
        if not roster:
            messagebox.showwarning(APP_TITLE, "Le tableau est vide.")
            return
        name = self.save_name_var.get().strip()
        if not name:
            messagebox.showwarning(APP_TITLE, "Donne un nom à cette classe avant de l'enregistrer.")
            return
        if name in class_store.list_classes():
            if not messagebox.askyesno(APP_TITLE, f"Une classe « {name} » existe déjà en mémoire. La remplacer ?"):
                return
        class_store.save_class(name, roster)
        self._refresh_store_combo(select=name)
        messagebox.showinfo(APP_TITLE, f"Classe « {name} » enregistrée en mémoire ({len(roster)} élève(s)).")

    # -- Application dans l'onglet appelant -----------------------------
    def _on_use_click(self):
        roster, errors = self._extract_roster()
        if errors:
            messagebox.showerror(APP_TITLE, "Corrige d'abord :\n\n" + "\n".join(errors))
            return
        if not roster:
            messagebox.showwarning(APP_TITLE, "Le tableau est vide.")
            return
        self.on_apply(roster, self.save_name_var.get().strip() or None)
        self.destroy()


# ---------------------------------------------------------------------
# Onglet 2 : scan et correction
# ---------------------------------------------------------------------
class ScanTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=12)
        self.settings = app_config.load_settings()
        self.roster = {}
        self.report = []
        self.answer_key = None
        self.results_win = None
        self._build_ui()

    def _build_ui(self):
        s = self.settings

        top = ttk.Frame(self)
        top.pack(fill="x")

        csv_frame = ttk.LabelFrame(top, text="1. Liste de la classe (CSV)", padding=10)
        csv_frame.pack(fill="x", pady=(0, 8))
        self.csv_path_var = tk.StringVar(value=s.get("scan_csv_path", ""))
        row = ttk.Frame(csv_frame)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.csv_path_var, width=50).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Parcourir…", command=self._browse_csv).pack(side="left", padx=5)
        ttk.Button(row, text="Créer un modèle…", command=lambda: offer_csv_template(self)).pack(
            side="left", padx=5)
        row1b = ttk.Frame(csv_frame)
        row1b.pack(fill="x", pady=(4, 0))
        ttk.Button(row1b, text="Tableau élèves (voir/modifier/mémoire)…",
                   command=self._open_roster_manager).pack(side="left")
        ttk.Button(row1b, text="Enregistrer cette classe en mémoire",
                   command=self._save_class_to_memory).pack(side="left", padx=5)
        self.csv_info_label = ttk.Label(csv_frame, text="", foreground="#555555")
        self.csv_info_label.pack(anchor="w", pady=(4, 0))
        if self.csv_path_var.get() and os.path.exists(self.csv_path_var.get()):
            self._load_csv(self.csv_path_var.get())

        photos_frame = ttk.LabelFrame(top, text="2. Photos des copies", padding=10)
        photos_frame.pack(fill="x", pady=(0, 8))
        btn_row = ttk.Frame(photos_frame)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Ajouter des photos…", command=self._add_photos).pack(side="left")
        ttk.Button(btn_row, text="Ajouter un dossier…", command=self._add_folder).pack(side="left", padx=5)
        ttk.Button(btn_row, text="Ajouter un PDF (copies scannées)…", command=self._add_pdf).pack(
            side="left", padx=5)
        ttk.Button(btn_row, text="Retirer la sélection", command=self._remove_selected_photos).pack(side="left", padx=5)
        ttk.Button(btn_row, text="Vider la liste", command=self._clear_photos).pack(side="left", padx=5)
        self.photos_list = tk.Listbox(photos_frame, height=6, selectmode="extended")
        self.photos_list.pack(fill="x", pady=6)

        key_row = ttk.Frame(photos_frame)
        key_row.pack(fill="x")
        ttk.Button(key_row, text="Définir le corrigé (facultatif)…", command=self._open_answer_key).pack(side="left")
        self.answer_key_label = ttk.Label(key_row, text="Corrigé : non défini (pas de notation)",
                                           foreground="#555555")
        self.answer_key_label.pack(side="left", padx=10)

        out_frame = ttk.LabelFrame(top, text="3. Dossier de résultats", padding=10)
        out_frame.pack(fill="x", pady=(0, 8))
        self.output_dir_var = tk.StringVar(
            value=s.get("scan_output_dir", os.path.join(app_config.default_documents_dir(), "Corrections")))
        row3 = ttk.Frame(out_frame)
        row3.pack(fill="x")
        ttk.Entry(row3, textvariable=self.output_dir_var, width=50).pack(side="left", fill="x", expand=True)
        ttk.Button(row3, text="Parcourir…", command=self._browse_output_dir).pack(side="left", padx=5)

        action_row = ttk.Frame(self)
        action_row.pack(fill="x", pady=6)
        self.run_btn = ttk.Button(action_row, text="Lancer la correction", command=self._on_run)
        self.run_btn.pack(side="left")
        self.progress = ttk.Progressbar(action_row, mode="indeterminate", length=200)
        self.progress.pack(side="left", padx=10)
        self.show_results_btn = ttk.Button(action_row, text="Voir les résultats…",
                                            command=self._ensure_results_window, state="disabled")
        self.show_results_btn.pack(side="left", padx=5)

        self.status_label = ttk.Label(self, text="", foreground="#555555")
        self.status_label.pack(fill="x", pady=(4, 0))

    def _ensure_results_window(self):
        """Les résultats (résumé + tableau) s'affichent dans une fenêtre
        séparée plutôt qu'en bas de cet onglet : sur un lot de plusieurs
        dizaines de copies, le tableau ne tenait pas dans la fenêtre
        principale et passait hors champ. Fermer cette fenêtre la cache
        seulement (elle garde son contenu) ; 'Voir les résultats…' la
        rouvre."""
        if self.results_win is not None and self.results_win.winfo_exists():
            self.results_win.deiconify()
            self.results_win.lift()
            return

        win = tk.Toplevel(self)
        win.title("Résultats de la correction")
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        win.geometry(f"{min(1000, int(screen_w * 0.8))}x{min(720, int(screen_h * 0.8))}")
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        self.results_win = win

        btn_row = ttk.Frame(win, padding=10)
        btn_row.pack(fill="x")
        self.export_btn = ttk.Button(btn_row, text="Exporter les résultats (CSV)", command=self._on_export)
        self.export_btn.pack(side="left")
        self.update_csv_btn = ttk.Button(btn_row, text="Mettre à jour le CSV classe", command=self._on_update_csv)
        self.update_csv_btn.pack(side="left", padx=5)

        self.summary_label = ttk.Label(win, text="", foreground="#333333", wraplength=960, justify="left",
                                        padding=(10, 0))
        self.summary_label.pack(fill="x")

        columns = ("numero", "eleve", "classe", "statut", "note")
        self.tree = ttk.Treeview(win, columns=columns, show="headings", height=20)
        for col, label, width in [("numero", "N°", 50), ("eleve", "Élève", 180), ("classe", "Classe", 80),
                                   ("statut", "Statut", 130), ("note", "Note", 90)]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=8)
        self.tree.bind("<Double-1>", self._on_row_double_click)
        self.tree.tag_configure("ok", background="#dff2df")
        self.tree.tag_configure("warn", background="#fdeccb")
        self.tree.tag_configure("unknown", background="#fddede")
        self.tree.tag_configure("missing", background="#e4e9f7")
        self.tree.tag_configure("error", background="#f6c6c6")
        ttk.Label(win, text="Double-clique sur une ligne pour voir/corriger le détail d'une copie.",
                  foreground="#555555").pack(anchor="w", padx=10, pady=(0, 10))

        self._populate_tree()

    # -- CSV --------------------------------------------------------
    def _browse_csv(self):
        path = filedialog.askopenfilename(title="Choisir le fichier CSV de la classe",
                                           filetypes=[("Fichiers CSV", "*.csv"), ("Tous les fichiers", "*.*")])
        if path:
            self._load_csv(path)

    def _load_csv(self, path):
        try:
            roster, use_path, message = smart_load_roster_with_export(path)
        except (OSError, KeyError, ValueError) as exc:
            messagebox.showerror(APP_TITLE, f"Impossible de lire ce fichier CSV.\n\nDétail : {exc}")
            return
        if message:
            messagebox.showinfo(APP_TITLE, message)
        self.csv_path_var.set(use_path)
        self.roster = roster
        self.csv_info_label.config(text=f"{len(roster)} élève(s) chargé(s).")

    def _open_roster_manager(self):
        def on_apply(roster, name):
            self.roster = roster
            self.csv_path_var.set(f"(classe en mémoire : {name})" if name else "(tableau modifié manuellement)")
            self.csv_info_label.config(text=f"{len(roster)} élève(s) chargé(s).")

        RosterManagerDialog(self, initial_roster=self.roster,
                             initial_name=suggest_class_name(self.roster) if self.roster else None,
                             on_apply=on_apply)

    def _save_class_to_memory(self):
        roster = self.roster
        if not roster:
            messagebox.showwarning(APP_TITLE, "Charge d'abord une liste d'élèves (CSV) avant de l'enregistrer "
                                                "en mémoire.")
            return
        suggested = suggest_class_name(roster)
        name = simpledialog.askstring(APP_TITLE, "Nom de la classe à enregistrer en mémoire :",
                                       initialvalue=suggested, parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in class_store.list_classes():
            if not messagebox.askyesno(APP_TITLE, f"Une classe « {name} » existe déjà en mémoire. La remplacer ?"):
                return
        class_store.save_class(name, roster)
        messagebox.showinfo(APP_TITLE, f"Classe « {name} » enregistrée en mémoire ({len(roster)} élève(s)).")

    # -- Photos -------------------------------------------------------
    def _add_photos(self):
        paths = filedialog.askopenfilenames(
            title="Choisir les photos des copies",
            filetypes=[("Images", " ".join(f"*{ext}" for ext in IMAGE_EXTS)), ("Tous les fichiers", "*.*")])
        for p in paths:
            self.photos_list.insert("end", p)

    def _add_folder(self):
        d = filedialog.askdirectory(title="Choisir un dossier de photos")
        if not d:
            return
        found = []
        for ext in IMAGE_EXTS:
            found.extend(glob.glob(os.path.join(d, f"*{ext}")))
            found.extend(glob.glob(os.path.join(d, f"*{ext.upper()}")))
        for p in sorted(set(found)):
            self.photos_list.insert("end", p)

    def _add_pdf(self):
        path = filedialog.askopenfilename(title="Choisir le PDF des copies scannées",
                                           filetypes=[("Fichier PDF", "*.pdf"), ("Tous les fichiers", "*.*")])
        if not path:
            return
        self.progress.start(12)
        self.status_label.config(text=f"Extraction des pages de « {os.path.basename(path)} »…")

        def work():
            import fitz
            doc = fitz.open(path)
            try:
                n_pages = doc.page_count
                if n_pages == 0:
                    raise ValueError("Ce PDF ne contient aucune page.")
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
            self.status_label.config(text="")
            if err:
                exc, tb = err
                messagebox.showerror(APP_TITLE, f"Impossible de lire ce PDF :\n\n{exc}")
                return
            for p in result:
                self.photos_list.insert("end", p)
            messagebox.showinfo(APP_TITLE, f"{len(result)} page(s) extraite(s) de « {os.path.basename(path)} » "
                                            "et ajoutée(s) à la liste des photos.")

        run_in_background(self, work, on_done)

    def _remove_selected_photos(self):
        for idx in reversed(self.photos_list.curselection()):
            self.photos_list.delete(idx)

    def _clear_photos(self):
        self.photos_list.delete(0, "end")

    # -- Corrigé --------------------------------------------------------
    def _open_answer_key(self):
        s = self.settings
        n_q = s.get("n_questions", 12)
        n_c = s.get("n_choices", 5)
        dialog = AnswerKeyDialog(self, n_q, n_c, existing_key=self.answer_key)
        self.wait_window(dialog)
        if dialog.result is not None:
            self.answer_key = dialog.result or None
            s["n_questions"] = int(dialog.n_q_var.get())
            s["n_choices"] = int(dialog.n_choices_var.get())
            app_config.save_settings(s)
            if self.answer_key:
                self.answer_key_label.config(text=f"Corrigé : défini ({len(self.answer_key)} question(s) notée(s))")
            else:
                self.answer_key_label.config(text="Corrigé : non défini (pas de notation)")

    # -- Dossier de sortie ---------------------------------------------
    def _browse_output_dir(self):
        d = filedialog.askdirectory(title="Choisir le dossier de résultats")
        if d:
            self.output_dir_var.set(d)

    # -- Lancer la correction -------------------------------------------
    def _on_run(self):
        photo_paths = list(self.photos_list.get(0, "end"))
        if not photo_paths:
            messagebox.showwarning(APP_TITLE, "Ajoute au moins une photo de copie avant de lancer la correction.")
            return
        if not self.roster:
            if not messagebox.askyesno(APP_TITLE, "Aucun fichier CSV classe chargé : tous les numéros seront "
                                                    "signalés comme inconnus. Continuer ?"):
                return
        output_dir = self.output_dir_var.get().strip()
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Impossible d'utiliser ce dossier de résultats : {exc}")
            return

        self.run_btn.config(state="disabled")
        self.progress.start(12)
        self._ensure_results_window()
        self.summary_label.config(text=f"Lecture de {len(photo_paths)} photo(s) en cours…")

        def work():
            run_dir = os.path.join(output_dir, "correction_" + time.strftime("%Y%m%d_%H%M%S"))
            report = process_batch(photo_paths, self.roster, run_dir)
            if self.answer_key:
                scoring.compute_scores(report, self.answer_key)
            return report, run_dir

        def on_done(err, result):
            self.progress.stop()
            self.run_btn.config(state="normal")
            if err:
                exc, tb = err
                messagebox.showerror(APP_TITLE, f"Échec de la correction :\n\n{exc}")
                self.summary_label.config(text="")
                return
            report, run_dir = result
            self.report = report
            self.last_run_dir = run_dir
            self._ensure_results_window()
            self._populate_tree()
            self.show_results_btn.config(state="normal")
            s = self.settings
            s["scan_csv_path"] = self.csv_path_var.get()
            s["scan_output_dir"] = self.output_dir_var.get()
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
            note = entry.get("note")
            note_str = "-"
            if note is not None:
                note_str = f"{note:.2f}/20"
                if entry.get("incertaines"):
                    note_str += f" (⚠ {len(entry['incertaines'])})"
            self.tree.insert("", "end", iid=str(idx),
                              values=(numero, eleve, classe, STATUS_LABELS.get(status, status), note_str),
                              tags=(tag_map.get(status, "error"),))

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
        default_dir = getattr(self, "last_run_dir", self.output_dir_var.get())
        path = filedialog.asksaveasfilename(
            title="Exporter les résultats", initialdir=default_dir,
            initialfile=f"resultats_{time.strftime('%Y%m%d_%H%M%S')}.csv",
            defaultextension=".csv", filetypes=[("Fichier CSV", "*.csv")])
        if not path:
            return
        max_q = 0
        for e in self.report:
            if e.get("questions"):
                max_q = max(max_q, max(e["questions"].keys()))
        has_notes = any(e.get("note") is not None for e in self.report)
        headers = ["numero", "eleve", "classe", "statut"]
        if has_notes:
            headers += ["note", "questions_incertaines"]
        headers += [f"Q{q}" for q in range(1, max_q + 1)]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(headers)
            for e in self.report:
                row = [e.get("sheet_number", ""), e.get("eleve", ""), e.get("classe", ""),
                       STATUS_LABELS.get(e.get("status"), e.get("status"))]
                if has_notes:
                    note = e.get("note")
                    row.append(f"{note:.2f}" if note is not None else "")
                    row.append(",".join(str(q) for q in e.get("incertaines", [])))
                questions = e.get("questions", {})
                for q in range(1, max_q + 1):
                    qres = questions.get(q)
                    row.append(";".join(qres["answers"]) if qres else "")
                w.writerow(row)
        if messagebox.askyesno(APP_TITLE, f"Résultats exportés :\n{path}\n\nOuvrir le fichier ?"):
            os.startfile(path)

    def _on_update_csv(self):
        path = self.csv_path_var.get()
        # csv_path_var affiche parfois un texte informatif plutôt qu'un
        # vrai chemin (ex. "(classe en mémoire : 6A)" après un chargement
        # depuis la mémoire) : ne jamais l'utiliser tel quel comme fichier
        # à écrire, toujours demander un emplacement dans ce cas.
        if not path or not path.lower().endswith(".csv"):
            path = filedialog.asksaveasfilename(title="Enregistrer le CSV classe", defaultextension=".csv",
                                                 filetypes=[("Fichier CSV", "*.csv")])
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
            messagebox.showerror(APP_TITLE, f"Impossible d'enregistrer le CSV : {exc}")
            return
        self.roster = merged
        self.csv_path_var.set(path)
        self.csv_info_label.config(text=f"{len(merged)} élève(s) chargé(s).")
        messagebox.showinfo(APP_TITLE, f"CSV classe mis à jour : {path}")


def main():
    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("880x820")

    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass

    header = ttk.Frame(root, padding=(12, 10, 12, 0))
    header.pack(fill="x")
    ttk.Label(header, text="QCM Scanner", font=("Segoe UI", 14, "bold")).pack(anchor="w")
    ttk.Label(header, text="Génération des feuilles-réponses et correction automatique des copies scannées.",
              foreground="#555555").pack(anchor="w")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=12, pady=10)
    notebook.add(GenerateTab(notebook), text="Générer les feuilles")
    notebook.add(ScanTab(notebook), text="Scanner / Corriger")

    root.mainloop()


if __name__ == "__main__":
    main()

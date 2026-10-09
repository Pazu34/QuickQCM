"""
QCM Scanner Mobile -- Android companion to the desktop QCM Scanner app.

First version, scope deliberately limited to SCANNING AND GRADING on
the phone (no sheet generation, no on-phone class/answer-key editor):
1. Import a class roster (CSV) and/or an answer key (JSON), both
   exported from the desktop app -- see README.md for the exact
   format, which is simply the desktop app's own file format.
2. Take photos of copies with the phone's camera and get them read and
   graded on the spot, using EXACTLY the same detection/scoring engine
   as the desktop app (detect_modular.py, roster_match.py, scoring.py
   -- copied here unchanged, see those files' own docstrings).
3. End the session: results are archived locally in the SAME folder
   layout as the desktop app's "Données" folder (class_archive.py,
   copied unchanged too), and can be exported/shared as a .csv + .json
   pair to bring back onto the computer (drop them into the matching
   class's "Corrections" folder and the desktop app's archive browser
   picks them right up, no import step needed there).

This file intentionally keeps everything in one module for a first
version: one state object (AppState), one Kivy Screen class per step of
the flow, and the App class itself with its KV layout as a single
string at the bottom. Split it up once the app grows past this.
"""
import os
import shutil
import threading
import time

from kivy.utils import platform

if platform != "android":
    # Previews the phone-portrait layout when running/testing this app
    # on a desktop computer (the window is whatever the device screen
    # is on Android, so this only matters off-device).
    from kivy.config import Config
    Config.set("graphics", "width", "420")
    Config.set("graphics", "height", "800")

from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.properties import StringProperty, BooleanProperty, ObjectProperty
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.popup import Popup
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button

import app_config
import answer_key_store
import class_archive
import roster_match
import scoring

APP_TITLE = "QCM Scanner Mobile"

# Human-readable French labels for roster_match's status values -- a
# small, phone-only stand-in for the desktop app's full translations.py
# (not needed here: this first version is French-only).
STATUS_LABELS = {
    "ok": "OK",
    "feuille_douteuse": "Feuille douteuse (à vérifier)",
    "numero_inconnu": "Numéro inconnu dans la classe",
    "face_manquante": "Verso manquant",
    "erreur_lecture": "Erreur de lecture",
}


def status_label(status):
    return STATUS_LABELS.get(status, status or "?")


def note_text(entry):
    note = entry.get("note")
    points = entry.get("points")
    max_points = entry.get("max_points")
    if note is not None:
        return f"{note:.2f}/20"
    if points is not None and max_points is not None:
        return f"{points:g}/{max_points:g}"
    return "-"


class AppState:
    """Everything the screens share: the loaded class/roster, the
    loaded answer key (optional -- scanning works without one, just
    without a grade), and the list of copies scanned in the current
    session (kept in memory until "Terminer la séance", which is when
    they're actually written to the archive -- so a session can be
    freely corrected/retried before anything is saved for good)."""

    def __init__(self):
        self.classe_name = None
        self.roster = {}
        self.answer_key_name = None
        self.answer_key = None
        self.session_entries = []  # list of graded report entries, this session only
        self.pending_sources = []  # photo(s) taken for the sheet currently being scanned

    def reset_session(self):
        self.session_entries = []
        self.pending_sources = []

    def scans_dir(self):
        d = os.path.join(app_config.get_config_dir(), "_scans_tmp")
        os.makedirs(d, exist_ok=True)
        return d

    def set_classe(self, name, roster):
        self.classe_name = name
        self.roster = roster
        s = app_config.load_settings()
        s["last_classe"] = name
        app_config.save_settings(s)

    def set_answer_key(self, name, answer_key):
        self.answer_key_name = name
        self.answer_key = answer_key
        s = app_config.load_settings()
        s["last_answer_key"] = name
        app_config.save_settings(s)

    def restore_last_session(self):
        """Reloads the class/answer key used the last time the app was
        open (remembered via settings.json), so the teacher doesn't
        have to re-import the same CSV/JSON file every single time she
        opens the app -- only once per class/corrigé, ever. Both are
        already persisted for real by class_archive/answer_key_store;
        settings.json only remembers WHICH one to reload by name."""
        s = app_config.load_settings()
        last_classe = s.get("last_classe")
        if last_classe:
            roster = class_archive.load_roster(last_classe)
            if roster:
                self.classe_name = last_classe
                self.roster = roster
        last_key = s.get("last_answer_key")
        if last_key:
            answer_key = answer_key_store.load_key(last_key)
            if answer_key:
                self.answer_key_name = last_key
                self.answer_key = answer_key


STATE = AppState()


def show_message(title, text):
    content = BoxLayout(orientation="vertical", padding=16, spacing=12)
    label = Label(text=text, halign="left", valign="top")
    label.bind(size=lambda inst, _s: setattr(inst, "text_size", (inst.width, None)))
    content.add_widget(label)
    popup = Popup(title=title, content=content, size_hint=(0.85, 0.5))
    close_btn = Button(text="OK", size_hint=(1, None), height="48dp")
    content.add_widget(close_btn)
    close_btn.bind(on_release=popup.dismiss)
    popup.open()
    return popup


def pick_file(patterns, on_selected):
    """Lets the user choose one existing file. `on_selected` is called
    with a path, or None if cancelled. Uses Android's native document
    picker on-device (via plyer -- this is what lets the teacher grab a
    file from anywhere: Drive, Downloads, an email attachment...), and
    a plain built-in Kivy file browser on desktop, where plyer has no
    backend installed -- handy for testing this app off-device."""
    if platform == "android":
        from plyer import filechooser

        try:
            filechooser.open_file(on_selection=on_selected, filters=patterns)
        except Exception as exc:
            show_message("Sélection de fichier indisponible", str(exc))
            on_selected(None)
        return

    from kivy.uix.filechooser import FileChooserListView

    content = BoxLayout(orientation="vertical", spacing=8, padding=8)
    chooser = FileChooserListView(filters=patterns, path=os.path.expanduser("~"))
    content.add_widget(chooser)
    btn_row = BoxLayout(size_hint=(1, None), height="48dp", spacing=8)
    content.add_widget(btn_row)
    popup = Popup(title="Choisir un fichier", content=content, size_hint=(0.95, 0.9))

    def confirm(*_a):
        selection = list(chooser.selection)
        popup.dismiss()
        on_selected(selection if selection else None)

    def cancel(*_a):
        popup.dismiss()
        on_selected(None)

    ok_btn = Button(text="Choisir")
    cancel_btn = Button(text="Annuler")
    ok_btn.bind(on_release=confirm)
    cancel_btn.bind(on_release=cancel)
    btn_row.add_widget(cancel_btn)
    btn_row.add_widget(ok_btn)
    popup.open()


def ask_text(title, hint, initial, on_confirm):
    from kivy.uix.textinput import TextInput

    content = BoxLayout(orientation="vertical", padding=16, spacing=12)
    inp = TextInput(text=initial or "", hint_text=hint, multiline=False, size_hint=(1, None), height="48dp")
    content.add_widget(inp)
    btn_row = BoxLayout(size_hint=(1, None), height="48dp", spacing=8)
    content.add_widget(btn_row)
    popup = Popup(title=title, content=content, size_hint=(0.9, 0.4))

    def confirm(*_a):
        value = inp.text.strip()
        popup.dismiss()
        if value:
            on_confirm(value)

    ok_btn = Button(text="Valider")
    cancel_btn = Button(text="Annuler")
    ok_btn.bind(on_release=confirm)
    cancel_btn.bind(on_release=popup.dismiss)
    btn_row.add_widget(cancel_btn)
    btn_row.add_widget(ok_btn)
    popup.open()


# ---------------------------------------------------------------------
# Camera / file picking -- native camera on Android (via plyer), a
# plain file picker on desktop so the whole flow can be exercised and
# screenshotted WITHOUT a phone (point it at a sample sheet image).
# ---------------------------------------------------------------------
def capture_photo(on_photo):
    """Calls `on_photo(path_or_None)` once a photo is available."""
    dest = os.path.join(STATE.scans_dir(), f"scan_{int(time.time() * 1000)}.jpg")
    if platform == "android":
        from plyer import camera

        def done(*_a):
            on_photo(dest if os.path.isfile(dest) else None)

        try:
            camera.take_picture(filename=dest, on_complete=done)
        except Exception as exc:
            show_message("Caméra indisponible", str(exc))
            on_photo(None)
    else:
        def done(selection):
            if selection:
                shutil.copy(selection[0], dest)
                on_photo(dest)
            else:
                on_photo(None)

        pick_file(["*.png", "*.jpg", "*.jpeg"], done)


# ---------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------
class HomeScreen(Screen):
    classe_summary = StringProperty("Aucune classe importée.")
    key_summary = StringProperty("Aucun corrigé importé (les copies seront lues sans note).")
    can_scan = BooleanProperty(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.refresh()  # STATE may already hold a class/key restored from the last session

    def on_pre_enter(self, *_a):
        self.refresh()

    def refresh(self):
        if STATE.classe_name:
            self.classe_summary = f"Classe chargée : {STATE.classe_name} ({len(STATE.roster)} élève(s))"
        else:
            self.classe_summary = "Aucune classe importée."
        if STATE.answer_key_name:
            n_q = len(STATE.answer_key.get("questions", {})) if STATE.answer_key else 0
            self.key_summary = f"Corrigé chargé : {STATE.answer_key_name} ({n_q} question(s) notée(s))"
        else:
            self.key_summary = "Aucun corrigé importé (les copies seront lues sans note)."
        self.can_scan = bool(STATE.classe_name)

    def import_class_csv(self):
        def done(selection):
            if not selection:
                return
            try:
                roster = roster_match.load_roster(selection[0])
            except (OSError, KeyError, ValueError) as exc:
                show_message("Import impossible", f"Fichier CSV illisible : {exc}")
                return
            if not roster:
                show_message("Import impossible", "Aucun élève trouvé dans ce fichier.")
                return
            suggested = next(iter(roster.values())).get("classe") or ""
            if not suggested:
                suggested = os.path.splitext(os.path.basename(selection[0]))[0]

            def confirmed(name):
                class_archive.save_roster(name, roster)
                STATE.set_classe(name, roster)
                self.refresh()
                show_message("Classe importée", f"« {name} » : {len(roster)} élève(s).")

            ask_text("Nom de la classe", "ex. 6A", suggested, confirmed)

        pick_file(["*.csv"], done)

    def import_key_json(self):
        import json

        def done(selection):
            if not selection:
                return
            try:
                with open(selection[0], encoding="utf-8") as f:
                    raw = json.load(f)
                questions = {int(q): {"correct": list(v.get("correct", [])),
                                       "points": float(v.get("points", 1.0))}
                             for q, v in raw.get("questions", {}).items()}
                answer_key = {"questions": questions,
                              "negative_points": bool(raw.get("negative_points", False)),
                              "partial_credit": bool(raw.get("partial_credit", False))}
            except (OSError, ValueError, AttributeError, TypeError) as exc:
                show_message("Import impossible", f"Fichier de corrigé illisible : {exc}")
                return
            if not answer_key["questions"]:
                show_message("Import impossible", "Ce corrigé ne contient aucune question notée.")
                return
            suggested = os.path.splitext(os.path.basename(selection[0]))[0]

            def confirmed(name):
                answer_key_store.save_key(name, answer_key)
                STATE.set_answer_key(name, answer_key)
                self.refresh()
                show_message("Corrigé importé", f"« {name} » : {len(answer_key['questions'])} question(s).")

            ask_text("Nom du corrigé", "ex. Controle_ch3", suggested, confirmed)

        pick_file(["*.json"], done)

    def goto_scan(self):
        if not STATE.classe_name:
            return
        STATE.reset_session()
        self.manager.current = "scan"

    def goto_session(self):
        self.manager.current = "session"


class ScanScreen(Screen):
    status_text = StringProperty("Prêt à scanner une copie.")
    photo_path = StringProperty("")
    result_visible = BooleanProperty(False)
    result_text = StringProperty("")
    busy = BooleanProperty(False)

    def on_pre_enter(self, *_a):
        self.status_text = f"Classe : {STATE.classe_name} -- {len(STATE.session_entries)} copie(s) validée(s)."
        self.photo_path = ""
        self.result_visible = False
        STATE.pending_sources = []

    def take_photo(self):
        self.busy = True
        self.status_text = "Ouverture de l'appareil photo…"

        def on_photo(path):
            def apply(*_a):
                self.busy = False
                if path is None:
                    self.status_text = "Aucune photo prise."
                    return
                STATE.pending_sources.append(path)
                self.photo_path = path
                self.status_text = f"{len(STATE.pending_sources)} photo(s) prête(s) -- analyse en cours…"
                self.analyse()
            Clock.schedule_once(apply, 0)

        capture_photo(on_photo)

    def analyse(self):
        sources = list(STATE.pending_sources)

        def work():
            try:
                report = roster_match.process_batch(sources, STATE.roster, output_dir=STATE.scans_dir())
            except Exception as exc:  # noqa: BLE001 -- show it, never crash the app
                Clock.schedule_once(lambda *_a: self._on_error(str(exc)), 0)
                return
            if STATE.answer_key:
                scoring.compute_scores(report, STATE.answer_key)
            Clock.schedule_once(lambda *_a: self._on_analysed(report), 0)

        threading.Thread(target=work, daemon=True).start()

    def _on_error(self, message):
        self.busy = False
        self.status_text = "Erreur pendant l'analyse."
        show_message("Erreur de lecture", message)

    def _on_analysed(self, report):
        self.busy = False
        entry = report[0] if report else None
        if entry is None:
            self.status_text = "Rien détecté sur cette photo."
            return

        if entry.get("status") == "face_manquante":
            self.status_text = "Verso manquant -- prends aussi une photo du verso de cette copie."
            self._current_entry = None
            self.result_visible = False
            return

        self._current_entry = entry
        lines = [f"N° {entry.get('sheet_number', '?')} -- {status_label(entry.get('status'))}"]
        if entry.get("eleve"):
            lines.append(f"Élève : {entry['eleve']}")
        if entry.get("note") is not None or entry.get("points") is not None:
            lines.append(f"Note : {note_text(entry)}")
        incertaines = entry.get("incertaines")
        if incertaines:
            lines.append("Questions incertaines : " + ", ".join(str(q) for q in incertaines))
        questions = entry.get("questions") or {}
        answers_line = ", ".join(
            f"Q{q}={''.join(r.get('answers', [])) or '-'}" for q, r in sorted(questions.items())
        )
        if answers_line:
            lines.append(answers_line)
        self.result_text = "\n".join(lines)
        self.result_visible = True
        self.status_text = "Vérifie le résultat puis valide, ou reprends la photo."

    def validate(self):
        entry = getattr(self, "_current_entry", None)
        if entry is None:
            return
        STATE.session_entries.append(entry)
        self.on_pre_enter()

    def retake(self):
        self.on_pre_enter()

    def back_home(self):
        self.manager.current = "home"


class SessionScreen(Screen):
    summary_text = StringProperty("")
    list_text = StringProperty("")

    def on_pre_enter(self, *_a):
        self.refresh()

    def refresh(self):
        entries = STATE.session_entries
        n_ok = sum(1 for e in entries if e.get("status") == "ok")
        self.summary_text = f"{len(entries)} copie(s) scannée(s) cette séance ({n_ok} associée(s) à un élève)."
        lines = []
        for e in entries:
            name = e.get("eleve") or f"n°{e.get('sheet_number', '?')} ({status_label(e.get('status'))})"
            lines.append(f"{name} : {note_text(e)}")
        self.list_text = "\n".join(lines) if lines else "Aucune copie validée pour l'instant."

    def export_session(self):
        if not STATE.session_entries:
            show_message("Rien à exporter", "Aucune copie validée dans cette séance.")
            return

        def confirmed(run_name):
            headers = ["numero", "eleve", "classe", "statut", "note"]
            rows = []
            for e in STATE.session_entries:
                rows.append([e.get("sheet_number", ""), e.get("eleve", ""), e.get("classe", ""),
                             status_label(e.get("status")), note_text(e)])
            csv_path = class_archive.save_results(STATE.classe_name, run_name, headers, rows)
            class_archive.save_report_json(STATE.classe_name, run_name, STATE.session_entries,
                                            answer_key=STATE.answer_key)
            run_folder = class_archive.run_dir(STATE.classe_name, run_name)
            self._try_share(csv_path)
            show_message(
                "Séance enregistrée",
                f"Résultats enregistrés dans :\n{run_folder}\n\n"
                "Copie ce dossier sur ton ordinateur, dans le même sous-dossier de classe de "
                "l'application PC (Données/Classes/<classe>/Corrections/), pour le retrouver "
                "automatiquement dans « Gestion des données »."
            )
            STATE.reset_session()
            self.manager.current = "home"

        ask_text("Nom de cette correction", "ex. Controle_chapitre_3",
                  time.strftime("Correction_%Y-%m-%d"), confirmed)

    def _try_share(self, csv_path):
        if platform != "android":
            return
        try:
            from plyer import share
            share.share(title="Résultats QCM", text="Résultats de la séance de scan.", filepath=csv_path)
        except Exception:
            pass  # the save-location message shown right after is the reliable fallback

    def back_home(self):
        self.manager.current = "home"


KV = """
#:import dp kivy.metrics.dp

ScreenManager:
    HomeScreen:
    ScanScreen:
    SessionScreen:

<HomeScreen>:
    name: "home"
    BoxLayout:
        orientation: "vertical"
        padding: dp(20)
        spacing: dp(14)

        Label:
            text: "QCM Scanner Mobile"
            font_size: "24sp"
            size_hint_y: None
            height: dp(40)
            bold: True

        Label:
            text: root.classe_summary
            size_hint_y: None
            height: dp(60)
            text_size: self.width, None
            halign: "left"
            valign: "middle"

        Label:
            text: root.key_summary
            size_hint_y: None
            height: dp(60)
            text_size: self.width, None
            halign: "left"
            valign: "middle"
            color: 0.4, 0.4, 0.4, 1

        Button:
            text: "Importer une classe (CSV)"
            size_hint_y: None
            height: dp(56)
            on_release: root.import_class_csv()

        Button:
            text: "Importer un corrigé (JSON)"
            size_hint_y: None
            height: dp(56)
            on_release: root.import_key_json()

        Widget:
            size_hint_y: None
            height: dp(10)

        Button:
            text: "Scanner une copie"
            size_hint_y: None
            height: dp(72)
            font_size: "20sp"
            disabled: not root.can_scan
            background_color: (0.2, 0.55, 0.3, 1) if root.can_scan else (0.6, 0.6, 0.6, 1)
            on_release: root.goto_scan()

        Button:
            text: "Résultats de la séance en cours"
            size_hint_y: None
            height: dp(48)
            on_release: root.goto_session()

        Widget:

<ScanScreen>:
    name: "scan"
    BoxLayout:
        orientation: "vertical"
        padding: dp(20)
        spacing: dp(12)

        Label:
            text: root.status_text
            size_hint_y: None
            height: dp(70)
            text_size: self.width, None
            halign: "left"
            valign: "middle"

        Image:
            source: root.photo_path
            size_hint_y: 0.35
            allow_stretch: True

        ScrollView:
            size_hint_y: 0.35
            Label:
                text: root.result_text
                text_size: self.width, None
                size_hint_y: None
                height: self.texture_size[1]
                halign: "left"
                valign: "top"
                opacity: 1 if root.result_visible else 0

        BoxLayout:
            size_hint_y: None
            height: dp(60)
            spacing: dp(10)
            opacity: 1 if root.result_visible else 0
            disabled: not root.result_visible

            Button:
                text: "Reprendre la photo"
                on_release: root.retake()

            Button:
                text: "Valider"
                background_color: 0.2, 0.55, 0.3, 1
                on_release: root.validate()

        Button:
            text: "Prendre une photo"
            size_hint_y: None
            height: dp(72)
            font_size: "20sp"
            disabled: root.busy
            on_release: root.take_photo()

        Button:
            text: "Retour"
            size_hint_y: None
            height: dp(48)
            on_release: root.back_home()

<SessionScreen>:
    name: "session"
    BoxLayout:
        orientation: "vertical"
        padding: dp(20)
        spacing: dp(12)

        Label:
            text: "Résultats de la séance"
            font_size: "20sp"
            bold: True
            size_hint_y: None
            height: dp(40)

        Label:
            text: root.summary_text
            size_hint_y: None
            height: dp(40)

        ScrollView:
            Label:
                text: root.list_text
                text_size: self.width, None
                size_hint_y: None
                height: self.texture_size[1]
                halign: "left"
                valign: "top"

        Button:
            text: "Terminer la séance et enregistrer"
            size_hint_y: None
            height: dp(64)
            background_color: 0.2, 0.55, 0.3, 1
            on_release: root.export_session()

        Button:
            text: "Retour"
            size_hint_y: None
            height: dp(48)
            on_release: root.back_home()
"""


class QcmScannerMobileApp(App):
    def build(self):
        self.title = APP_TITLE
        if platform == "android":
            self._request_android_permissions()
        STATE.restore_last_session()
        return Builder.load_string(KV)

    def _request_android_permissions(self):
        try:
            from android.permissions import request_permissions, Permission
            request_permissions([Permission.CAMERA])
        except Exception:
            pass


if __name__ == "__main__":
    QcmScannerMobileApp().run()

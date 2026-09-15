"""Per-class archive under the portable "Données" folder (see
app_config.get_config_dir()): one subfolder per class holding that
class's roster, the results of every grading run, and -- only if the
teacher opted in -- a space-saving copy of the scanned answer sheets.
This is independent of ScanTab's user-chosen "results folder" (which can
point anywhere, e.g. a shared drive): this archive always lives inside
the app's own portable data folder, so it travels with the USB drive no
matter where the teacher sends the working output.

This is also the SOLE storage for "classes saved to memory" (roster
save/load/rename/delete, from GenerateTab, ScanTab and
RosterManagerDialog) -- a class saved there is the exact same class
shown/editable in DataTab, not a separate copy. It replaces the older
class_store.py (a single flat classes.json); see
migrate_legacy_class_store() below for the one-time upgrade path.

Layout, for a class named "6A":
    Données/
      Classes/
        6A/
          eleves.csv                     <- roster (numero;nom;classe)
          Corrections/
            Controle_chapitre_3/         <- name chosen by the teacher (or
              resultats.csv                 a date-based default), see
              report.json                   qcm_app.build_run_name()
              Copies/                    <- only if "save copies" was ticked
                n5_Dupont.jpg
                n6_Martin.jpg
                ...

report.json holds the full report structure (not just the flattened
resultats.csv), so an archived correction can be reloaded into the app
with full fidelity -- per-bubble flagged/confidence detail included --
for the "load an archived correction" feature (see qcm_app.py's
ArchiveBrowserDialog)."""
import csv
import json
import os
import shutil
import time

import cv2

import app_config

ARCHIVE_ROOT_NAME = "Classes"
CORRECTIONS_DIRNAME = "Corrections"
COPIES_DIRNAME = "Copies"
ROSTER_FILENAME = "eleves.csv"
RESULTS_FILENAME = "resultats.csv"
REPORT_JSON_FILENAME = "report.json"

# Archived copies are downscaled JPEGs: legible enough on screen to
# re-check a student's marks by eye, while keeping the archive from
# growing as large as the original 300 DPI scans (typically a few
# hundred KB per copy instead of several MB).
COPY_MAX_SIDE_PX = 1600
COPY_JPEG_QUALITY = 82

_UNSAFE_CHARS = '<>:"/\\|?*'


def _sanitize(name):
    cleaned = "".join(ch for ch in name if ch not in _UNSAFE_CHARS).strip()
    return cleaned or "?"


def _class_dir_path(classe_name):
    """Path only, WITHOUT creating it -- used by the listing functions so
    just browsing the archive (nothing selected yet) doesn't leave empty
    folders behind."""
    return os.path.join(app_config.get_config_dir(), ARCHIVE_ROOT_NAME, _sanitize(classe_name))


def class_dir(classe_name):
    """This class's archive folder, created if missing."""
    path = _class_dir_path(classe_name)
    os.makedirs(path, exist_ok=True)
    return path


def run_dir(classe_name, run_name):
    """This grading run's archive folder (one per correction launched
    for that class), created if missing."""
    path = os.path.join(class_dir(classe_name), CORRECTIONS_DIRNAME, _sanitize(run_name))
    os.makedirs(path, exist_ok=True)
    return path


def list_classes():
    """Names of every class that has an archive folder, alphabetically."""
    root = os.path.join(app_config.get_config_dir(), ARCHIVE_ROOT_NAME)
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))


def rename_class(old_name, new_name):
    """Renames a class's whole archive folder (roster AND its
    corrections history move together). Returns True on success, False
    if there's nothing to rename or a class with `new_name` already
    exists (folders aren't merged automatically)."""
    old_path = _class_dir_path(old_name)
    new_path = _class_dir_path(new_name)
    if old_name == new_name or not os.path.isdir(old_path) or os.path.exists(new_path):
        return False
    os.rename(old_path, new_path)
    return True


def delete_class(name):
    """Permanently deletes a class's whole archive folder (roster AND
    every correction/copy archived for it)."""
    path = _class_dir_path(name)
    if os.path.isdir(path):
        shutil.rmtree(path)


_LEGACY_STORE_FILENAME = "classes.json"


def migrate_legacy_class_store():
    """One-time migration from the old class_store.py storage (a single
    Données/classes.json file with every roster, superseded by this
    per-class-folder archive so "classes saved to memory" and "saved
    data" are the same thing everywhere in the app) into this module's
    per-class eleves.csv. Renames the legacy file afterwards so this only
    ever does real work once. Safe to call on every startup -- a no-op
    once migrated (or if there was nothing to migrate)."""
    legacy_path = os.path.join(app_config.get_config_dir(), _LEGACY_STORE_FILENAME)
    if not os.path.isfile(legacy_path):
        return
    try:
        with open(legacy_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return
    existing = set(list_classes())
    for name, roster_raw in raw.items():
        if name in existing:
            continue  # a class_archive folder already exists for this name -- don't overwrite it
        try:
            roster = {int(num): {"nom": info.get("nom", ""), "classe": info.get("classe", "")}
                      for num, info in roster_raw.items()}
        except (TypeError, ValueError, AttributeError):
            continue
        save_roster(name, roster)
    try:
        os.replace(legacy_path, legacy_path + ".migrated")
    except OSError:
        pass


def list_corrections(classe_name):
    """Names of every archived correction for this class, most recently
    saved first."""
    corr_root = os.path.join(_class_dir_path(classe_name), CORRECTIONS_DIRNAME)
    if not os.path.isdir(corr_root):
        return []
    entries = [d for d in os.listdir(corr_root) if os.path.isdir(os.path.join(corr_root, d))]
    entries.sort(key=lambda d: os.path.getmtime(os.path.join(corr_root, d)), reverse=True)
    return entries


def save_roster(classe_name, roster):
    """Writes/updates this class's eleves.csv (numero;nom;classe) --
    always kept in sync so it's found alongside that class's results,
    for a teacher browsing the archive directly in Windows Explorer."""
    path = os.path.join(class_dir(classe_name), ROSTER_FILENAME)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["numero", "nom", "classe"])
        for num in sorted(roster.keys()):
            info = roster[num]
            w.writerow([num, info.get("nom", ""), info.get("classe", classe_name)])
    return path


def load_roster(classe_name):
    """Reads this class's eleves.csv back into a roster dict {numero
    (int): {"nom":..., "classe":...}}, for the "review/complete this
    class's data" tab (see qcm_app.DataTab). Returns {} if this class
    hasn't been archived yet."""
    path = os.path.join(_class_dir_path(classe_name), ROSTER_FILENAME)
    if not os.path.isfile(path):
        return {}
    roster = {}
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            try:
                num = int(row["numero"])
            except (KeyError, ValueError, TypeError):
                continue
            roster[num] = {"nom": row.get("nom", "").strip(), "classe": row.get("classe", "").strip()}
    return roster


def correction_stats(classe_name, run_name):
    """Quick stats for one archived correction -- number of copies
    processed, average grade out of 20 (only counting entries that were
    actually graded, i.e. an answer key was in use for that run), and
    when it was saved. Used for the class-by-class analysis view (see
    qcm_app.DataTab). Returns None if that correction doesn't exist."""
    path = os.path.join(_class_dir_path(classe_name), CORRECTIONS_DIRNAME, _sanitize(run_name))
    if not os.path.isdir(path):
        return None
    date_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
    report = load_report_json(classe_name, run_name)
    if report is None:
        return {"n_entries": None, "avg_note_20": None, "date": date_str}
    notes = [e["note"] for e in report if e.get("note") is not None]
    return {
        "n_entries": len(report),
        "avg_note_20": (sum(notes) / len(notes)) if notes else None,
        "date": date_str,
    }


def save_results(classe_name, run_name, headers, rows):
    """Archives one grading run's results as a CSV under this class's
    Corrections/<run_name>/ folder. Past evaluations accumulate here,
    one subfolder per run, so the class's whole history is visible at a
    glance."""
    path = os.path.join(run_dir(classe_name, run_name), RESULTS_FILENAME)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(headers)
        w.writerows(rows)
    return path


def save_copies(classe_name, run_name, report):
    """Saves a compressed (downscaled + JPEG) copy of every scanned
    source image referenced in `report` -- including read-error entries,
    whose original photo is often exactly what a teacher needs to look
    at to understand why the automatic reading failed. Returns the list
    of paths written."""
    dest_dir = os.path.join(run_dir(classe_name, run_name), COPIES_DIRNAME)
    os.makedirs(dest_dir, exist_ok=True)
    saved = []
    for entry in report:
        sources = entry.get("sources")
        if sources is None:
            single = entry.get("source")
            sources = [single] if single else []
        num = entry.get("sheet_number")
        eleve = entry.get("eleve", "")
        for i, src in enumerate(sources):
            img = cv2.imread(src)
            if img is None:
                continue
            h, w = img.shape[:2]
            longest = max(h, w)
            if longest > COPY_MAX_SIDE_PX:
                scale = COPY_MAX_SIDE_PX / longest
                new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
                img = cv2.resize(img, new_size, interpolation=cv2.INTER_AREA)
            if num is not None:
                name = f"n{num}" + (f"_{_sanitize(eleve)}" if eleve else "")
            else:
                name = _sanitize(os.path.splitext(os.path.basename(src))[0])
            if len(sources) > 1:
                name += f"_{i + 1}"
            out_path = os.path.join(dest_dir, name + ".jpg")
            cv2.imwrite(out_path, img, [cv2.IMWRITE_JPEG_QUALITY, COPY_JPEG_QUALITY])
            saved.append(out_path)
    return saved


def save_report_json(classe_name, run_name, report):
    """Archives the full report (list of dicts, as returned by
    roster_match.process_batch / scoring.compute_scores) as JSON, so
    load_report_json() can later reload it with full fidelity -- unlike
    resultats.csv, which only keeps a flattened, display-oriented view."""
    path = os.path.join(run_dir(classe_name, run_name), REPORT_JSON_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path


def load_report_json(classe_name, run_name):
    """Reloads a report archived by save_report_json(). Returns None if
    no report.json exists for that run (e.g. an older archive, or the
    file was removed by hand)."""
    path = os.path.join(_class_dir_path(classe_name), CORRECTIONS_DIRNAME, _sanitize(run_name),
                         REPORT_JSON_FILENAME)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        report = json.load(f)
    for entry in report:
        # JSON object keys are always strings -- "questions" is keyed by
        # question NUMBER everywhere else in the app, so convert back.
        if entry.get("questions"):
            entry["questions"] = {int(q): r for q, r in entry["questions"].items()}
    return report

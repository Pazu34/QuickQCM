"""
Sheet-number <-> student matching, from a table (2-column CSV:
numero,nom -- French for "number,name" -- with an optional classe
["class"] column; these field names are used as-is throughout the data
model, see also generate_sheet_modular.py and qcm_app.py).

Natively handles double-sided sheets: two photos (front + back) bearing
the same sheet number are automatically merged into a single result per
student, covering the full set of questions.

Typical usage:

    roster = load_roster("classe_6eH.csv")
    results = process_batch(["photo1.jpg", "photo2.jpg", ...], roster,
                             output_dir="/mnt/user-data/outputs/corrections")

`results` contains, for each STUDENT (not each photo), either the
matched name and all their answers, or a flag (unknown number, doubtful
sheet, missing side) together with a cropped image of the Name/Class
area for manual review. Status values and format_batch_report()'s
output text are intentionally left in French (see qcm_app.py's
status_label() for the translated display layer)."""
import csv
import os
import sys
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from detect_modular import analyse, LowResolutionError


def load_roster(csv_path):
    """Reads a CSV with at least the 'numero' and 'nom' columns (and
    optionally 'classe'). Returns {numero (int): {"nom":..., "classe":...}}."""
    roster = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            num = int(row["numero"])
            roster[num] = {
                "nom": row.get("nom", "").strip(),
                "classe": row.get("classe", "").strip() if "classe" in row else "",
            }
    return roster


def process_batch(image_paths, roster, output_dir, adaptive=True):
    """Processes a list of photos (one image = one SIDE of a sheet -- a
    single-sided sheet has only one image, a double-sided one has two).
    Automatically groups the sides of the same sheet (same decoded
    number) into a single result per student, then matches it via the
    table. Reports problem cases:
    - decoded number absent from the table -> Name/Class area image
      saved for manual review
    - double-sided sheet with a side not provided/read -> flagged
    - sheet with no clean separation on at least one side -> flagged
    - reading error (markers, resolution) -> flagged with the original
      photo's path

    Returns a list of dicts, one per STUDENT/sheet (not per photo).
    """
    os.makedirs(output_dir, exist_ok=True)
    unmatched_dir = os.path.join(output_dir, "a_verifier")
    os.makedirs(unmatched_dir, exist_ok=True)

    # --- Pass 1: read each photo individually ---
    read_errors = []
    by_sheet = {}
    for path in image_paths:
        try:
            result = analyse(path, adaptive=adaptive)
        except (LowResolutionError, ValueError) as e:
            read_errors.append({"source": path, "status": "erreur_lecture", "erreur": str(e)})
            continue
        num = result["sheet_number"]
        by_sheet.setdefault(num, []).append({"source": path, "result": result})

    # --- Pass 2: merge the sides of the same sheet, then match ---
    report = []
    for num, entries in by_sheet.items():
        first = entries[0]["result"]
        rv = first["is_recto_verso"]
        sides_found = {e["result"]["side"] for e in entries}

        merged_questions = {}
        needs_review = False
        identity_crop = None
        for e in entries:
            res = e["result"]
            merged_questions.update(res["questions"])
            needs_review = needs_review or res["sheet_needs_manual_review"]
            if identity_crop is None:
                identity_crop = res["identity_crop"]

        entry = {
            "sheet_number": num,
            "sources": [e["source"] for e in entries],
            "questions": merged_questions,
        }

        missing_sides = ({0, 1} - sides_found) if rv else set()
        if missing_sides:
            entry["status"] = "face_manquante"
            entry["missing_sides"] = sorted(missing_sides)
            crop_path = os.path.join(unmatched_dir, f"n{num}_face_manquante.png")
            cv2.imwrite(crop_path, identity_crop)
            entry["identity_crop_path"] = crop_path
            report.append(entry)
            continue

        student = roster.get(num)
        if student is None:
            entry["status"] = "numero_inconnu"
            crop_path = os.path.join(unmatched_dir, f"n{num}_inconnu.png")
            cv2.imwrite(crop_path, identity_crop)
            entry["identity_crop_path"] = crop_path
        elif needs_review:
            entry["status"] = "feuille_douteuse"
            entry["eleve"] = student["nom"]
            crop_path = os.path.join(unmatched_dir, f"n{num}_{student['nom']}_douteuse.png")
            cv2.imwrite(crop_path, identity_crop)
            entry["identity_crop_path"] = crop_path
        else:
            entry["status"] = "ok"
            entry["eleve"] = student["nom"]
            entry["classe"] = student.get("classe", "")

        report.append(entry)

    return read_errors + report


def format_batch_report(report):
    lines = []
    n_ok = sum(1 for e in report if e["status"] == "ok")
    n_unknown = sum(1 for e in report if e["status"] == "numero_inconnu")
    n_doubtful = sum(1 for e in report if e["status"] == "feuille_douteuse")
    n_missing = sum(1 for e in report if e["status"] == "face_manquante")
    n_error = sum(1 for e in report if e["status"] == "erreur_lecture")

    lines.append(f"{len(report)} feuille(s)/élève(s) traité(s) : {n_ok} associée(s), "
                 f"{n_unknown} numéro(s) inconnu(s), {n_doubtful} feuille(s) douteuse(s), "
                 f"{n_missing} face(s) manquante(s), {n_error} erreur(s) de lecture.")
    lines.append("")

    for e in report:
        if e["status"] == "ok":
            lines.append(f"\u2705 n\u00b0{e['sheet_number']} -> {e['eleve']} "
                         f"({len(e['questions'])} question(s) lue(s))")
        elif e["status"] == "numero_inconnu":
            lines.append(f"\u2753 n\u00b0{e['sheet_number']} -> AUCUN ÉLÈVE CORRESPONDANT "
                         f"dans le tableau (voir {e['identity_crop_path']})")
        elif e["status"] == "feuille_douteuse":
            lines.append(f"\u26a0\ufe0f n\u00b0{e['sheet_number']} ({e['eleve']}) -> feuille sans "
                         f"séparation nette, à corriger à la main "
                         f"(voir {e['identity_crop_path']})")
        elif e["status"] == "face_manquante":
            faces = ", ".join("recto" if s == 0 else "verso" for s in e["missing_sides"])
            lines.append(f"\U0001f4c4 n\u00b0{e['sheet_number']} -> face(s) manquante(s) : {faces} "
                         f"(voir {e['identity_crop_path']})")
        else:
            lines.append(f"\u274c {e['source']} -> erreur de lecture : {e['erreur']}")

    return "\n".join(lines)

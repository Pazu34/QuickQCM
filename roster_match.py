"""
Correspondance numéro de feuille <-> élève, à partir d'un tableau
(CSV à 2 colonnes : numero,nom — une colonne classe optionnelle).

Gère nativement le recto-verso : deux photos (recto + verso) portant le
même numéro de feuille sont automatiquement fusionnées en un seul
résultat par élève, couvrant l'ensemble des questions.

Utilisation typique :

    roster = load_roster("classe_6eH.csv")
    results = process_batch(["photo1.jpg", "photo2.jpg", ...], roster,
                             output_dir="/mnt/user-data/outputs/corrections")

`results` contient, pour chaque ÉLÈVE (pas chaque photo), soit le nom
associé et toutes ses réponses, soit un signalement (numéro inconnu,
feuille douteuse, face manquante) accompagné d'une image recadrée de la
zone Nom/Classe pour vérification manuelle.
"""
import csv
import os
import sys
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from detect_modular import analyse, LowResolutionError


def load_roster(csv_path):
    """Lit un CSV avec au moins les colonnes 'numero' et 'nom' (et
    optionnellement 'classe'). Retourne {numero (int): {"nom":..., "classe":...}}."""
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
    """Traite une liste de photos (une image = une FACE d'une feuille —
    une feuille simple face n'a qu'une image, une feuille recto-verso en
    a deux). Regroupe automatiquement les faces d'une même feuille (même
    numéro décodé) en un seul résultat par élève, puis l'associe via le
    tableau. Remonte les cas problématiques :
    - numéro décodé absent du tableau -> image de la zone Nom/Classe
      sauvegardée pour vérification manuelle
    - feuille recto-verso dont une face n'a pas été fournie/lue -> signalée
    - feuille sans séparation nette sur au moins une face -> signalée
    - erreur de lecture (repères, résolution) -> signalée avec le chemin
      de la photo d'origine

    Retourne une liste de dicts, un par ÉLÈVE/feuille (pas par photo).
    """
    os.makedirs(output_dir, exist_ok=True)
    unmatched_dir = os.path.join(output_dir, "a_verifier")
    os.makedirs(unmatched_dir, exist_ok=True)

    # --- Passe 1 : lire chaque photo individuellement ---
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

    # --- Passe 2 : fusionner les faces d'une même feuille, puis associer ---
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

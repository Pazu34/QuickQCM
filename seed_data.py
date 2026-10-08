"""Ships a small, deletable "Test" dataset (one class, one corrigé, one
already-graded correction) with the application, so a teacher can try
every feature -- the archive, the statistics view, re-opening a
correction -- before ever scanning a real copy.

Created once per data folder (see app_config.get_config_dir()): the
teacher can delete the class/corrigé/correction like any other of her
own (there's nothing special about them once created), and it is NOT
recreated afterwards, thanks to the "test_dataset_seeded" flag kept in
that data folder's settings.json. A freshly chosen EMPTY data folder
(see qcm_app's "Dossier de données" controls) gets its own copy of the
dataset the next time the application starts, same as a brand new
install would.
"""
import random

import answer_key_store
import app_config
import class_archive
import scoring
import translations

TEST_CLASS_NAME = "Test"
TEST_KEY_NAME = "Test"
TEST_RUN_NAME = "Test"

_NAMES = [
    "Dupont Alice", "Martin Bob", "Durand Chloé", "Lefèvre David", "Moreau Emma",
    "Simon Félix", "Laurent Gaëlle", "Michel Hugo", "Garcia Inès", "Bernard Jules",
    "Robert Kelly", "Richard Louis", "Petit Marie", "Dubois Noah", "Dupuis Olivia",
    "Fournier Paul", "Girard Quentin", "Bonnet Romane", "François Sacha", "Henry Tess",
    "Roussel Ugo", "Mathieu Victor", "Gautier Wendy", "Perrin Xavier", "Morel Yasmine",
]

_N_QUESTIONS = 12
_LETTERS = ["A", "B", "C", "D"]

# How many of the 25 students answer each question correctly -- picked
# so the TOTAL per student ends up roughly bell-shaped (sum of 12
# mostly-moderate, mostly-independent per-question difficulties), with
# question 1 intentionally almost always right and question 8
# intentionally almost always missed, as requested.
_CORRECT_COUNTS = {
    1: 24, 2: 17, 3: 14, 4: 19, 5: 11, 6: 15,
    7: 9, 8: 3, 9: 16, 10: 13, 11: 18, 12: 12,
}

# Fixed seed: the shipped dataset looks the same (same names get the
# same grades) whether it's seeded on this machine or another -- it's a
# reference demo, not something that needs to vary run to run.
_SEED = 20241001


def _build_roster():
    return {i + 1: {"nom": nom, "classe": TEST_CLASS_NAME} for i, nom in enumerate(_NAMES)}


def _build_answer_key():
    questions = {q: {"correct": [_LETTERS[(q - 1) % len(_LETTERS)]], "points": 1.0}
                 for q in range(1, _N_QUESTIONS + 1)}
    return {"questions": questions, "negative_points": False, "partial_credit": False}


def _build_report(answer_key):
    rng = random.Random(_SEED)
    n_students = len(_NAMES)

    # Who gets each question right, chosen independently question by
    # question so a student's TOTAL is the sum of many near-independent
    # yes/no draws (-> roughly normal across the class, like a real
    # grade distribution).
    correct_students = {}
    for q in range(1, _N_QUESTIONS + 1):
        order = list(range(n_students))
        rng.shuffle(order)
        correct_students[q] = set(order[:_CORRECT_COUNTS[q]])

    report = []
    for idx, nom in enumerate(_NAMES):
        questions = {}
        for q in range(1, _N_QUESTIONS + 1):
            correct_letter = answer_key["questions"][q]["correct"][0]
            if idx in correct_students[q]:
                chosen = correct_letter
            else:
                wrong_choices = [l for l in _LETTERS if l != correct_letter]
                chosen = wrong_choices[(idx + q) % len(wrong_choices)]
            questions[q] = {"answers": [chosen], "flagged": [], "confidence": {}}
        report.append({
            "sheet_number": idx + 1,
            "sources": [],
            "questions": questions,
            "status": "ok",
            "eleve": nom,
            "classe": TEST_CLASS_NAME,
        })
    scoring.compute_scores(report, answer_key)
    return report


def _build_results_csv_rows(report):
    tr = translations.tr
    headers = ["numero", "eleve", "classe", "statut", "note", "questions_incertaines"]
    headers += [f"Q{q}" for q in range(1, _N_QUESTIONS + 1)]
    rows = []
    for e in report:
        row = [e["sheet_number"], e["eleve"], e["classe"], tr("status_ok"),
               f"{e['points']:g}/{e['max_points']:g}", ""]
        for q in range(1, _N_QUESTIONS + 1):
            row.append(";".join(e["questions"][q]["answers"]))
        rows.append(row)
    return headers, rows


def ensure_test_dataset():
    """Creates the "Test" class/corrigé/correction the first time this
    data folder is used by the application. A no-op on every later call
    (including after the teacher deletes the test data -- it isn't
    brought back)."""
    settings = app_config.load_settings()
    if settings.get("test_dataset_seeded"):
        return
    if TEST_CLASS_NAME not in class_archive.list_classes() and TEST_KEY_NAME not in answer_key_store.list_keys():
        class_archive.save_roster(TEST_CLASS_NAME, _build_roster())
        answer_key = _build_answer_key()
        answer_key_store.save_key(TEST_KEY_NAME, answer_key)
        report = _build_report(answer_key)
        headers, rows = _build_results_csv_rows(report)
        class_archive.save_results(TEST_CLASS_NAME, TEST_RUN_NAME, headers, rows)
        class_archive.save_report_json(TEST_CLASS_NAME, TEST_RUN_NAME, report, answer_key=answer_key)
    settings["test_dataset_seeded"] = True
    app_config.save_settings(settings)

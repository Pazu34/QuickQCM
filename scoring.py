"""
Grading: compares the detected answers (produced by
roster_match.process_batch / detect_modular.analyse) to an answer key
entered by the teacher in the interface.

This module is an addition (not one of the 4 already-tested modules): it
doesn't touch detection, only comparison + grade computation.
"""


def compute_scores(report, answer_key):
    """Adds 'points', 'max_points', 'note' (French for "grade", out of
    20) and 'incertaines' (French for "uncertain [ones]") to every
    report entry that has a 'questions' field (so not to 'erreur_lecture'
    nor 'face_manquante' entries, which have no usable answers). These
    field names are kept as-is to match the data model used throughout
    roster_match.py and qcm_app.py.

    answer_key: {question_number (int): list/set of correct letters,
    e.g. {1: ["B"], 2: ["A", "C"]}}. A question is counted correct only
    if the set of detected answers matches the expected set EXACTLY (no
    partial credit).

    A question where at least one bubble is "to check" (flagged) is
    counted as uncertain rather than correct or wrong: it's listed in
    'incertaines' and earns no points until the teacher has resolved it
    manually (via the detail window in the application).

    Modifies and returns `report` (list of dicts, see roster_match)."""
    max_points = len(answer_key)
    for entry in report:
        if "questions" not in entry:
            continue
        detected = entry["questions"]
        points = 0
        incertaines = []
        for qnum, correct in answer_key.items():
            qres = detected.get(qnum)
            if qres is None:
                incertaines.append(qnum)
                continue
            if qres.get("flagged"):
                incertaines.append(qnum)
                continue
            if set(qres.get("answers", [])) == set(correct):
                points += 1
        entry["points"] = points
        entry["max_points"] = max_points
        entry["note"] = round(points / max_points * 20, 2) if max_points else None
        entry["incertaines"] = incertaines
    return report


def rescore_entry(entry, answer_key):
    """Recomputes the grade for ONE single entry (after a manual fix in
    the detail window). Assumes 'flagged' was cleared for the questions
    resolved by hand (see DetailDialog)."""
    compute_scores([entry], answer_key)
    return entry

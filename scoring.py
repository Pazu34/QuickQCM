"""Grading: compares the detected answers (produced by
roster_match.process_batch / detect_modular.analyse) to an answer key
entered by the teacher in the interface (see answer_key_store.py for its
shape).

This module is an addition (not one of the 4 already-tested modules): it
doesn't touch detection, only comparison + grade computation.
"""


def _question_score(qres, correct, points, negative_points, partial_credit):
    """Score for a single question, given its config (correct answer(s),
    point value/barème) and the two grading options:

    - negative_points: a wrong (non-blank) answer scores -points instead
      of 0 -- symmetric with a correct answer's +points, to discourage
      guessing. A BLANK answer is never penalized either way.
    - partial_credit: for a question with MORE THAN ONE correct letter,
      gives proportional credit instead of all-or-nothing -- one point
      share per correct letter found, minus one share per wrongly
      marked letter, floored at 0 (e.g. correct={A,C}, student picks
      {A}: 1 of 2 shares -> points/2; picks {A,B}: 1 right, 1 wrong ->
      (1-1)/2 shares -> 0). Single-correct-answer questions are always
      all-or-nothing (there's nothing to partially credit)."""
    correct = set(correct)
    answers = set(qres.get("answers", []))
    if not answers:
        return 0.0
    if len(correct) > 1 and partial_credit:
        n_ok = len(answers & correct)
        n_bad = len(answers - correct)
        fraction = max(0.0, (n_ok - n_bad) / len(correct))
        return points * fraction
    if answers == correct:
        return points
    return -points if negative_points else 0.0


def compute_scores(report, answer_key):
    """Adds 'points', 'max_points' (both possibly fractional now that
    questions can carry a per-question point value/barème -- see
    answer_key_store.py), 'note' (French for "grade", out of 20) and
    'incertaines' (French for "uncertain [ones]") to every report entry
    that has a 'questions' field (so not to 'erreur_lecture' nor
    'face_manquante' entries, which have no usable answers). These field
    names are kept as-is to match the data model used throughout
    roster_match.py and qcm_app.py.

    A question where at least one bubble is "to check" (flagged), or that
    wasn't read at all, is counted as uncertain rather than scored: it's
    listed in 'incertaines' and earns no points until the teacher has
    resolved it manually (via the detail window in the application).

    The raw total (which can go negative with negative_points enabled) is
    floored at 0 for the final 'points' -- a bad guessing strategy across
    many questions shouldn't produce a negative overall grade, per common
    practice.

    Modifies and returns `report` (list of dicts, see roster_match)."""
    questions_cfg = answer_key.get("questions", {})
    negative_points = answer_key.get("negative_points", False)
    partial_credit = answer_key.get("partial_credit", False)
    max_points = sum(cfg.get("points", 1.0) for cfg in questions_cfg.values())
    for entry in report:
        if "questions" not in entry:
            continue
        detected = entry["questions"]
        raw_total = 0.0
        incertaines = []
        for qnum, cfg in questions_cfg.items():
            qres = detected.get(qnum)
            if qres is None or qres.get("flagged"):
                incertaines.append(qnum)
                continue
            raw_total += _question_score(qres, cfg.get("correct", []), cfg.get("points", 1.0),
                                          negative_points, partial_credit)
        entry["points"] = round(max(0.0, raw_total), 2)
        entry["max_points"] = round(max_points, 2)
        entry["note"] = round(entry["points"] / max_points * 20, 2) if max_points else None
        entry["incertaines"] = incertaines
    return report


def rescore_entry(entry, answer_key):
    """Recomputes the grade for ONE single entry (after a manual fix in
    the detail window). Assumes 'flagged' was cleared for the questions
    resolved by hand (see DetailDialog)."""
    compute_scores([entry], answer_key)
    return entry

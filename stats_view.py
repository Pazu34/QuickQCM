"""Data aggregation for the "Statistiques" mode of Gestion des données
(see qcm_app.DataTab): turns one or more archived corrections (see
class_archive.py) into the four breakdowns shown there -- by class, by
QCM (one bar per selected correction), by question, and by student.

Pure data crunching only, no Tkinter and no chart drawing here (that
part -- embedding matplotlib in the Tk window -- lives in qcm_app.py);
kept separate so the aggregation logic can be read/tested on its own.
"""
import class_archive


def list_all_corrections():
    """Every (classe, run_name) pair archived across every class,
    grouped by class (alphabetically) then most-recent-first within a
    class -- same order as class_archive.list_corrections()."""
    pairs = []
    for classe in class_archive.list_classes():
        for run_name in class_archive.list_corrections(classe):
            pairs.append((classe, run_name))
    return pairs


def load_selection(selection):
    """Loads (classe, run_name, report, answer_key) for each (classe,
    run_name) pair in `selection`. Skips a pair whose report.json is
    missing or empty (e.g. deleted by hand since the selection list was
    built) rather than raising -- the breakdowns below simply treat a
    shorter `loaded` list as "less data available"."""
    loaded = []
    for classe, run_name in selection:
        report = class_archive.load_report_json(classe, run_name)
        if not report:
            continue
        answer_key = class_archive.load_report_answer_key(classe, run_name)
        loaded.append((classe, run_name, report, answer_key))
    return loaded


def _graded_notes(report):
    return [e["note"] for e in report if e.get("note") is not None]


def average(values):
    return sum(values) / len(values) if values else None


def per_class(loaded):
    """{classe: [note, ...]} -- every graded student's note, pooled
    across every selected run of that class."""
    data = {}
    for classe, _run_name, report, _key in loaded:
        notes = _graded_notes(report)
        if notes:
            data.setdefault(classe, []).extend(notes)
    return data


def per_qcm(loaded):
    """{"classe – run_name": [note, ...]} -- one entry per selected
    correction, its own notes only (never merged across runs, unlike
    per_class)."""
    data = {}
    for classe, run_name, report, _key in loaded:
        notes = _graded_notes(report)
        if notes:
            data[f"{classe} – {run_name}"] = notes
    return data


def per_question(loaded):
    """{qnum: (n_correct, n_total)} pooled across every selected run
    THAT HAS an archived answer key (see
    class_archive.load_report_answer_key) -- a run saved before this
    feature existed, or graded without a corrigé selected, has no way
    to say what the correct answer was, so it's skipped for this
    breakdown only (the other three don't need the answer key)."""
    totals = {}
    for _classe, _run_name, report, answer_key in loaded:
        if not answer_key:
            continue
        questions_cfg = answer_key.get("questions", {})
        for entry in report:
            detected = entry.get("questions")
            if not detected:
                continue
            for qnum, cfg in questions_cfg.items():
                qres = detected.get(qnum)
                if qres is None or qres.get("flagged"):
                    continue
                n_correct, n_total = totals.get(qnum, (0, 0))
                is_correct = set(qres.get("answers", [])) == set(cfg.get("correct", []))
                totals[qnum] = (n_correct + (1 if is_correct else 0), n_total + 1)
    return totals


def per_student(loaded):
    """{eleve_name: [note, ...]} -- every graded note for that student,
    across however many of the selected runs they appear in (matched by
    name, since a numero is only unique within a single class/run)."""
    data = {}
    for _classe, _run_name, report, _key in loaded:
        for e in report:
            if e.get("note") is None:
                continue
            name = e.get("eleve")
            if name:
                data.setdefault(name, []).append(e["note"])
    return data

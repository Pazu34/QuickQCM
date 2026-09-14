"""
Notation : comparaison des réponses détectées (produites par
roster_match.process_batch / detect_modular.analyse) à un corrigé saisi
par l'enseignant dans l'interface.

Ce module est un ajout (pas un des 4 modules déjà testés) : il ne touche
pas à la détection, seulement à la comparaison + calcul de note.
"""


def compute_scores(report, answer_key):
    """Ajoute 'points', 'max_points', 'note' (/20) et 'incertaines' à
    chaque entrée du rapport qui possède un champ 'questions' (donc pas
    aux entrées 'erreur_lecture' ni 'face_manquante', qui n'ont pas de
    réponses exploitables).

    answer_key : {numero_question (int): liste/ensemble de lettres
    correctes, ex. {1: ["B"], 2: ["A", "C"]}}. Une question est comptée
    bonne seulement si l'ensemble des réponses détectées correspond
    EXACTEMENT à l'ensemble attendu (pas de crédit partiel).

    Une question dont au moins une bulle est "à vérifier" (flagged) est
    comptée comme incertaine plutôt que bonne ou mauvaise : elle est
    listée dans 'incertaines' et ne rapporte aucun point tant que
    l'enseignant ne l'a pas tranchée manuellement (via la fenêtre de
    détail dans l'application).

    Modifie et retourne `report` (liste de dicts, cf. roster_match)."""
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
    """Recalcule la note d'UNE seule entrée (après une correction
    manuelle dans la fenêtre de détail). Suppose que 'flagged' a été vidé
    pour les questions tranchées à la main (cf. DetailDialog)."""
    compute_scores([entry], answer_key)
    return entry

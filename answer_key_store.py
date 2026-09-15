"""Persistent storage for named answer keys ("corrigés"), one JSON file
per corrigé in a "Corrigés" folder inside the application's config
folder (see app_config.get_config_dir() -- a "Données" folder next to
the executable, portable-USB friendly). One file per corrigé (rather
than a single opaque blob) mirrors class_archive.py's "Classes" folder:
browsable, and each corrigé can be copied/backed up/inspected on its own
in Windows Explorer.

Lets the same corrigé -- per-question correct answer(s), per-question
point value (barème), and the negative-points/partial-credit grading
options -- be saved once and reused across several classes/batches of
copies of the same test, instead of re-entering it by hand every time.

An answer key is a dict:
    {
        "questions": {1: {"correct": ["A"], "points": 1.0}, ...},
        "negative_points": False,   # wrong answer -> -points (see scoring.py)
        "partial_credit": False,    # multi-answer question: proportional
                                     # credit instead of all-or-nothing
    }

Independent from the 4 already-tested modules and from scoring.py itself
(which only consumes this shape) -- touches neither generation, reading,
nor grading, only answer-key persistence between two runs of the
application."""
import json
import os

import app_config

STORE_DIRNAME = "Corrigés"
_UNSAFE_CHARS = '<>:"/\\|?*'


def _sanitize(name):
    cleaned = "".join(ch for ch in name if ch not in _UNSAFE_CHARS).strip()
    return cleaned or "?"


def _store_dir_path():
    """Path only, WITHOUT creating it -- so just listing corrigés (none
    saved yet) doesn't leave an empty folder behind."""
    return os.path.join(app_config.get_config_dir(), STORE_DIRNAME)


def _store_dir():
    path = _store_dir_path()
    os.makedirs(path, exist_ok=True)
    return path


def _key_path(name):
    return os.path.join(_store_dir(), _sanitize(name) + ".json")


def list_keys():
    d = _store_dir_path()
    if not os.path.isdir(d):
        return []
    return sorted(os.path.splitext(fn)[0] for fn in os.listdir(d) if fn.lower().endswith(".json"))


def load_key(name):
    path = os.path.join(_store_dir_path(), _sanitize(name) + ".json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        questions = {
            int(q): {"correct": list(info.get("correct", [])), "points": float(info.get("points", 1.0))}
            for q, info in raw.get("questions", {}).items()
        }
    except (TypeError, ValueError, AttributeError):
        return None
    return {
        "questions": questions,
        "negative_points": bool(raw.get("negative_points", False)),
        "partial_credit": bool(raw.get("partial_credit", False)),
    }


def save_key(name, answer_key):
    serializable = {
        "questions": {str(q): {"correct": info.get("correct", []), "points": info.get("points", 1.0)}
                      for q, info in answer_key.get("questions", {}).items()},
        "negative_points": bool(answer_key.get("negative_points", False)),
        "partial_credit": bool(answer_key.get("partial_credit", False)),
    }
    with open(_key_path(name), "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


def delete_key(name):
    path = os.path.join(_store_dir_path(), _sanitize(name) + ".json")
    if os.path.isfile(path):
        os.remove(path)


def rename_key(old_name, new_name):
    """Returns True on success, False if there's nothing to rename or a
    corrigé with `new_name` already exists."""
    old_path = os.path.join(_store_dir_path(), _sanitize(old_name) + ".json")
    new_path = os.path.join(_store_dir_path(), _sanitize(new_name) + ".json")
    if old_name == new_name or not os.path.isfile(old_path) or os.path.exists(new_path):
        return False
    os.rename(old_path, new_path)
    return True


_LEGACY_STORE_FILENAME = "corriges.json"


def migrate_legacy_store():
    """One-time migration from this module's very first storage format
    (a single Données/corriges.json blob) into the current one-file-per-
    corrigé "Corrigés" folder. Renames the legacy file afterwards so this
    only ever does real work once. Safe to call on every startup."""
    legacy_path = os.path.join(app_config.get_config_dir(), _LEGACY_STORE_FILENAME)
    if not os.path.isfile(legacy_path):
        return
    try:
        with open(legacy_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return
    existing = set(list_keys())
    for name, key_raw in raw.items():
        if name in existing:
            continue  # a corrigé file already exists for this name -- don't overwrite it
        try:
            questions = {
                int(q): {"correct": list(info.get("correct", [])), "points": float(info.get("points", 1.0))}
                for q, info in key_raw.get("questions", {}).items()
            }
        except (TypeError, ValueError, AttributeError):
            continue
        save_key(name, {
            "questions": questions,
            "negative_points": bool(key_raw.get("negative_points", False)),
            "partial_credit": bool(key_raw.get("partial_credit", False)),
        })
    try:
        os.replace(legacy_path, legacy_path + ".migrated")
    except OSError:
        pass

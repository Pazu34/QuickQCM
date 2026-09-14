"""Stockage persistant des tableaux de correspondance numero/nom/classe
("classes enregistrées en mémoire"), dans un fichier JSON du dossier de
configuration de l'application (%APPDATA%\\QCM Scanner\\classes.json).

Indépendant des 4 modules déjà testés : ne touche ni à la génération ni
à la lecture, seulement à la persistance des rosters entre deux
lancements de l'application."""
import json
import os

import app_config

STORE_FILENAME = "classes.json"


def _store_path():
    return os.path.join(app_config.get_config_dir(), STORE_FILENAME)


def _load_all():
    path = _store_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    result = {}
    for name, roster_raw in raw.items():
        try:
            result[name] = {int(num): info for num, info in roster_raw.items()}
        except (TypeError, ValueError):
            continue
    return result


def _save_all(data):
    serializable = {
        name: {str(num): {"nom": info.get("nom", ""), "classe": info.get("classe", "")}
               for num, info in roster.items()}
        for name, roster in data.items()
    }
    with open(_store_path(), "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


def list_classes():
    return sorted(_load_all().keys())


def load_class(name):
    return _load_all().get(name, {})


def save_class(name, roster):
    data = _load_all()
    data[name] = {num: {"nom": info.get("nom", ""), "classe": info.get("classe", "")}
                  for num, info in roster.items()}
    _save_all(data)


def delete_class(name):
    data = _load_all()
    data.pop(name, None)
    _save_all(data)


def rename_class(old_name, new_name):
    data = _load_all()
    if old_name in data and old_name != new_name:
        data[new_name] = data.pop(old_name)
        _save_all(data)

"""Petite persistance locale (dossier de sortie choisi, dernier corrigé,
dernier CSV classe...) pour éviter à l'utilisatrice de ressaisir la même
chose à chaque lancement. Fichier JSON dans %APPDATA%\\QCM Scanner."""
import json
import os

APP_DIR_NAME = "QCM Scanner"


def get_config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def default_documents_dir():
    docs = os.path.join(os.path.expanduser("~"), "Documents", APP_DIR_NAME)
    os.makedirs(docs, exist_ok=True)
    return docs


def _settings_path():
    return os.path.join(get_config_dir(), "settings.json")


def load_settings():
    path = _settings_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}
    return {}


def save_settings(data):
    try:
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

"""Petite persistance locale (dossier de sortie choisi, dernier corrigé,
dernier CSV classe, classes enregistrées en mémoire...) pour éviter à
l'utilisatrice de ressaisir la même chose à chaque lancement.

Tout est stocké dans un dossier "Données" À CÔTÉ DE L'EXÉCUTABLE (pas
dans %APPDATA%/Documents) : l'application est pensée pour tourner depuis
une clé USB, sur des ordinateurs différents d'une fois sur l'autre — ses
données (classes enregistrées, réglages, PDF générés, résultats de
correction) doivent donc voyager AVEC elle sur la clé plutôt que rester
éparpillées sur le premier ordinateur utilisé."""
import json
import os
import sys

DATA_DIR_NAME = "Données"


def get_base_dir():
    """Dossier de référence : celui de l'exécutable si l'application est
    empaquetée (PyInstaller, sys.frozen), sinon celui de ce fichier
    source (lancement depuis Python directement)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_config_dir():
    """Dossier "Données" à côté de l'exécutable. Si ce dossier n'est pas
    inscriptible (clé USB protégée en écriture, dossier en lecture
    seule...), on se rabat sur le dossier Documents de l'utilisateur
    plutôt que de planter."""
    d = os.path.join(get_base_dir(), DATA_DIR_NAME)
    try:
        os.makedirs(d, exist_ok=True)
        test_file = os.path.join(d, ".ecriture_test")
        with open(test_file, "w") as f:
            f.write("")
        os.remove(test_file)
        return d
    except OSError:
        fallback = os.path.join(os.path.expanduser("~"), "Documents", "QCM Scanner")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def default_documents_dir():
    return get_config_dir()


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

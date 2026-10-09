"""Android/Kivy equivalent of the desktop app's app_config.py: the same
tiny public surface (get_config_dir(), load_settings(), save_settings())
so class_archive.py and answer_key_store.py -- copied here UNCHANGED
from the desktop app -- work exactly as they do on PC. Only WHERE the
data folder lives differs, since "next to the executable" isn't a
meaningful concept on Android.

This first version always uses the app's own private storage (no
runtime permission needed, works immediately on every device). Letting
the teacher point the phone at an arbitrary shared folder (e.g. a
synced cloud folder, matching the PC app's customizable "Dossier de
données") is a natural next step, but needs Android's Storage Access
Framework and is left for a later version -- see README.md.
"""
import json
import os

DATA_DIR_NAME = "Donnees"


def get_base_dir():
    """The app's own private, always-writable storage folder. Falls
    back to a plain folder in the home directory when there's no
    running Kivy/Android app (e.g. a standalone script importing this
    module to inspect/test the data folder)."""
    try:
        from kivy.app import App
        app = App.get_running_app()
        if app is not None:
            return app.user_data_dir
    except Exception:
        pass
    fallback = os.path.join(os.path.expanduser("~"), ".qcmscan_data")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def get_config_dir():
    d = os.path.join(get_base_dir(), DATA_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


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

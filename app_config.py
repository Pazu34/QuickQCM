"""Small local persistence layer (chosen output folder, last answer key,
last class CSV, classes saved to memory...) so the teacher doesn't have
to re-enter the same things every time she launches the app.

Everything is stored in a "Données" folder NEXT TO THE EXECUTABLE (not
in %APPDATA%/Documents): the application is meant to run from a USB
drive, on different computers from one use to the next -- its data
(saved classes, settings, generated PDFs, grading results) must
therefore travel WITH it on the drive rather than stay scattered on
whichever computer was used first. "Données" (French for "Data") is
kept as the actual on-disk folder name across languages, for backward
compatibility with folders created by earlier versions of the app."""
import json
import os
import sys

DATA_DIR_NAME = "Données"


def get_base_dir():
    """Reference folder: the executable's if the application is
    packaged (PyInstaller, sys.frozen), otherwise this source file's
    (running directly from Python)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_config_dir():
    """"Données" folder next to the executable. If this folder isn't
    writable (write-protected USB drive, read-only folder...), falls
    back to the user's Documents folder rather than crashing."""
    d = os.path.join(get_base_dir(), DATA_DIR_NAME)
    try:
        os.makedirs(d, exist_ok=True)
        test_file = os.path.join(d, ".write_test")
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

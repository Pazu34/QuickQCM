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

# A teacher can point the app at a "Données" folder that ISN'T next to
# the executable (another USB drive, a shared network folder...). That
# choice can't be stored INSIDE settings.json, since settings.json's own
# location depends on it (chicken-and-egg) -- so it lives in this tiny
# separate pointer file, always next to the executable, holding only the
# chosen path. See get_config_dir()/set_data_dir_override().
_OVERRIDE_FILENAME = "data_dir.json"


def get_base_dir():
    """Reference folder: the executable's if the application is
    packaged (PyInstaller, sys.frozen), otherwise this source file's
    (running directly from Python)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def dir_is_usable(path):
    """True if `path` exists (or can be created) and is writable."""
    try:
        os.makedirs(path, exist_ok=True)
        test_file = os.path.join(path, ".write_test")
        with open(test_file, "w") as f:
            f.write("")
        os.remove(test_file)
        return True
    except OSError:
        return False


def _override_pointer_path():
    return os.path.join(get_base_dir(), _OVERRIDE_FILENAME)


def load_data_dir_override():
    """The custom data folder path chosen via set_data_dir_override(),
    or None if none was chosen (or the pointer file is unreadable)."""
    path = _override_pointer_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    chosen = (data.get("data_dir") or "").strip()
    return chosen or None


def set_data_dir_override(path):
    """Points the app at `path` as its data folder from now on (see
    qcm_app's "Dossier de données" controls in DataTab/PreferencesTab).
    Caller is expected to have checked dir_is_usable(path) first."""
    with open(_override_pointer_path(), "w", encoding="utf-8") as f:
        json.dump({"data_dir": path}, f, ensure_ascii=False, indent=2)


def clear_data_dir_override():
    """Reverts to the default "Données" folder next to the executable."""
    path = _override_pointer_path()
    if os.path.isfile(path):
        os.remove(path)


def get_config_dir():
    """The data folder to use: a custom one set via
    set_data_dir_override() if there is one and it's still usable,
    otherwise the "Données" folder next to the executable. If THAT
    isn't writable either (write-protected USB drive, read-only
    folder...), falls back to the user's Documents folder rather than
    crashing."""
    override = load_data_dir_override()
    if override and dir_is_usable(override):
        return override
    d = os.path.join(get_base_dir(), DATA_DIR_NAME)
    if dir_is_usable(d):
        return d
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

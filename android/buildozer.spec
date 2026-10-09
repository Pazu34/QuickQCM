[app]
title = QCM Scanner Mobile
package.name = qcmscannermobile
package.domain = org.quickqcm

source.dir = .
source.include_exts = py,png,jpg,jpeg,kv,json,csv,txt

version = 0.1.0

# Reuses the exact same detection/scoring/matching engine as the
# desktop app (opencv + numpy), plus plyer for the native camera/file
# picker and pyjnius for the small bits of direct Android API access
# (permissions, share sheet). No reportlab here: detect_modular.py
# imports generate_sheet_modular.py only for its sheet geometry/
# config-barcode constants, never its PDF-drawing functions (sheet
# GENERATION isn't exposed in this phone app yet, see README.md) --
# and reportlab's own Android build was unreliable in CI (its source
# host returned 403 Forbidden), so generate_sheet_modular.py now makes
# that import optional, dropped here accordingly.
requirements = python3,kivy==2.3.1,opencv,numpy,plyer,pyjnius

orientation = portrait
fullscreen = 0
android.permissions = CAMERA

android.api = 34
android.minapi = 24
android.archs = arm64-v8a
# Avoids the build hanging on an interactive license prompt in CI
# (buildozer accepts the Android SDK licenses on its own when true).
android.accept_sdk_license = True

[buildozer]
log_level = 2

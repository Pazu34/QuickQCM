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
# python3==3.11 (rather than letting p4a pick its newest default,
# currently 3.14): plyer and some of Kivy's own pip-installed
# dependencies have no python-for-android recipe and fall back to a
# plain `pip install` for the Android target -- on 3.14 this hit a
# live pip/packaging bug (a real charset_normalizer wheel built for
# "android_24_arm64_v8a"/cp314 that pip itself then refused to
# install, "not a supported wheel on this platform"). 3.11 is a far
# more established p4a target and doesn't hit this. hostpython3 (the
# Python that RUNS the build on the CI machine, as opposed to python3
# which runs ON the phone) must be pinned to the exact same version --
# p4a refuses to build otherwise ("python3 should have same version as
# hostpython3"). Needs the FULL version (major.minor.patch): p4a's
# hostpython3 recipe downloads straight from
# https://github.com/python/cpython/archive/refs/tags/v<version>.tar.gz,
# and "v3.11" alone isn't a real tag (404) -- only full ones are, e.g.
# v3.11.9.
requirements = python3==3.11.9,hostpython3==3.11.9,kivy==2.3.1,opencv,numpy,plyer,pyjnius

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

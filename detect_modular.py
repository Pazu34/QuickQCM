"""
Optical reading of the v2 sheet.

Simplifications compared to v1 (enabled by the new design):
- No more letter printed inside the bubbles -> no more need for
  per-letter correction (25th percentile), nor for row-relative
  comparison. Every bubble is judged on an absolute scale (local white
  -> local black), independently from the other bubbles in its row.
- Added sheet-number decoding (8-bit pattern, top-right corner).
- Added a flag for uncertain cases: any bubble whose darkness level
  falls in a gray zone (close to the decision threshold) is returned as
  "to check" rather than decided at random.

Corner detection and per-row white/black calibration are reused as-is
from detect.py (same marker geometry, hence same code). Status/output
text stays in French throughout this module (see qcm_app.py's
translated wrapper text and status_label() for the interface layer)."""
import sys
import os
import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sheet_layout_v2 import corner_points  # corner geometry, independent of cfg
import dataclasses
from generate_sheet_modular import (
    SheetConfig, bubble_centers_for, example_bubble_centers_for,
    blank_reference_points_for, id_bit_cell_centers_for, identity_zone_corners_for,
    config_barcode_bit_positions, decode_config_value, BARCODE_N_BITS,
    side_question_range, needs_recto_verso,
    CHOICES_MAX, BUBBLE_D, N_ID_BITS, bubble_d_native,
)


# ---------------------------------------------------------------------
# Corner detection and homography (identical to v1: same marker
# silhouette, hence same detection logic).
# ---------------------------------------------------------------------

def find_candidate_squares(gray):
    bw = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 35, 10
    )
    contours, hierarchy = cv2.findContours(bw, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    hierarchy = hierarchy[0] if hierarchy is not None else []

    candidates = []
    img_area = gray.shape[0] * gray.shape[1]
    for idx, cnt in enumerate(contours):
        area = cv2.contourArea(cnt)
        if area < img_area * 0.00015 or area > img_area * 0.03:
            continue
        (cx, cy), (rw, rh), angle = cv2.minAreaRect(cnt)
        if rw < 3 or rh < 3:
            continue
        aspect = max(rw, rh) / min(rw, rh)
        if aspect > 1.3:
            continue
        rect_area = rw * rh
        extent = area / rect_area if rect_area > 0 else 0
        if extent < 0.88:
            continue
        candidates.append({"idx": idx, "area": area, "center": (cx, cy)})
    return candidates, hierarchy, contours


def classify_marks(candidates, hierarchy):
    candidate_idxs = {c["idx"] for c in candidates}

    def has_candidate_ancestor(idx):
        parent = hierarchy[idx][3]
        while parent != -1:
            if parent in candidate_idxs:
                return True
            parent = hierarchy[parent][3]
        return False

    finders, plains = [], []
    for c in candidates:
        idx = c["idx"]
        child = hierarchy[idx][2]
        has_grandchild = False
        if child != -1:
            grandchild = hierarchy[child][2]
            has_grandchild = grandchild != -1
        if has_grandchild:
            finders.append(c)
        elif not has_candidate_ancestor(idx):
            plains.append(c)
    return finders, plains


def dedupe_by_center(items, min_dist=15):
    kept = []
    for it in sorted(items, key=lambda c: -c["area"]):
        if all(np.hypot(it["center"][0] - k["center"][0], it["center"][1] - k["center"][1]) > min_dist
               for k in kept):
            kept.append(it)
    return kept


def identify_corners(finders, plains, img_shape):
    pool = dedupe_by_center(list(finders) + list(plains))
    if len(pool) < 4:
        raise ValueError(
            f"Repères insuffisants détectés ({len(pool)}/4). "
            "Vérifie l'éclairage/le cadrage de la photo."
        )
    pool_sorted = sorted(pool, key=lambda c: -c["area"])
    big3 = pool_sorted[:3]
    rest = pool_sorted[3:]
    avg_big_area = sum(c["area"] for c in big3) / 3
    expected_br_area = 0.25 * avg_big_area
    if not rest:
        raise ValueError("4e repère (coin distinctif) introuvable.")
    br = min(rest, key=lambda c: abs(c["area"] - expected_br_area))

    def dist(a, b):
        return np.hypot(a["center"][0] - b["center"][0], a["center"][1] - b["center"][1])

    big3_sorted = sorted(big3, key=lambda f: -dist(f, br))
    tl = big3_sorted[0]
    remaining = sorted(big3_sorted[1:3], key=lambda f: -dist(f, br))
    tr, bl = remaining[0], remaining[1]
    return {"TL": tl["center"], "TR": tr["center"], "BL": bl["center"], "BR": br["center"]}


def build_homography(img_corners):
    canon = corner_points()
    src = np.float32([canon["TL"], canon["TR"], canon["BR"], canon["BL"]])
    dst = np.float32([img_corners["TL"], img_corners["TR"], img_corners["BR"], img_corners["BL"]])
    return cv2.getPerspectiveTransform(src, dst)


def mm_to_image(H, x_mm, y_mm):
    pt = np.float32([[[x_mm, y_mm]]])
    out = cv2.perspectiveTransform(pt, H)
    return float(out[0, 0, 0]), float(out[0, 0, 1])


def local_scale_px_per_mm(H, x_mm, y_mm):
    x0, y0 = mm_to_image(H, x_mm, y_mm)
    x1, y1 = mm_to_image(H, x_mm + 1.0, y_mm)
    return np.hypot(x1 - x0, y1 - y0)


class LowResolutionError(Exception):
    pass


MIN_PX_PER_MM = 5.0


def check_resolution(H):
    canon = corner_points()
    cx_mm = (canon["TL"][0] + canon["TR"][0]) / 2
    cy_mm = (canon["TL"][1] + canon["BL"][1]) / 2
    scale = local_scale_px_per_mm(H, cx_mm, cy_mm)
    if scale < MIN_PX_PER_MM:
        raise LowResolutionError(
            f"Résolution insuffisante autour des bulles (~{scale:.1f} px/mm, "
            f"minimum recommandé {MIN_PX_PER_MM:.0f} px/mm). "
            "Reprends la photo en te rapprochant de la feuille, ou en plus haute résolution."
        )
    return scale


# ---------------------------------------------------------------------
# Local contrast calibration (paper white + printed circles' black),
# same idea as v1.
# ---------------------------------------------------------------------

def read_mean_gray(gray, cx, cy, radius_px):
    r = max(4, int(round(radius_px)))
    x0, y0 = int(round(cx - r)), int(round(cy - r))
    x1, y1 = int(round(cx + r)), int(round(cy + r))
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(gray.shape[1], x1), min(gray.shape[0], y1)
    if x1 <= x0 or y1 <= y0:
        return 255.0
    patch = gray[y0:y1, x0:x1]
    yy, xx = np.mgrid[0:patch.shape[0], 0:patch.shape[1]]
    cx_local, cy_local = patch.shape[1] / 2, patch.shape[0] / 2
    mask = (xx - cx_local) ** 2 + (yy - cy_local) ** 2 <= r ** 2
    if mask.sum() == 0:
        return 255.0
    return float(patch[mask].mean())


def read_ring_darkness(gray, cx, cy, true_radius_px):
    r = true_radius_px
    x0, y0 = int(round(cx - r * 1.15)), int(round(cy - r * 1.15))
    x1, y1 = int(round(cx + r * 1.15)), int(round(cy + r * 1.15))
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(gray.shape[1], x1), min(gray.shape[0], y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    patch = gray[y0:y1, x0:x1].astype(np.float32)
    yy, xx = np.mgrid[0:patch.shape[0], 0:patch.shape[1]]
    cx_local, cy_local = patch.shape[1] / 2, patch.shape[0] / 2
    dist = np.sqrt((xx - cx_local) ** 2 + (yy - cy_local) ** 2)
    ring_mask = (dist >= r * 0.85) & (dist <= r * 1.15)
    if ring_mask.sum() == 0:
        return 0.0
    return float(np.percentile(patch[ring_mask], 5))


def illumination_at(coeffs, y_mm):
    return float(np.polyval(coeffs, y_mm))


def fit_illumination_curve(gray, H, cfg, side=0, degree=2):
    """White (blank paper to the right of the last column) + black
    (printed circles' outline) of each row, fitted with a polynomial as
    a function of height on the sheet."""
    blank_pts = blank_reference_points_for(cfg, side=side)
    bubble_pts = bubble_centers_for(cfg, side=side)
    _, n_side = side_question_range(cfg, side)
    bd = bubble_d_native(dataclasses.replace(cfg, n_questions=n_side))

    ys, whites, blacks = [], [], []
    for q, (x_mm, y_mm) in blank_pts.items():
        scale = local_scale_px_per_mm(H, x_mm, y_mm)
        radius_px = 2.0 * scale
        cx, cy = mm_to_image(H, x_mm, y_mm)
        white_val = read_mean_gray(gray, cx, cy, radius_px)

        black_vals = []
        for (bx_mm, by_mm) in bubble_pts[q]:
            true_r = (bd / 2) * scale
            bcx, bcy = mm_to_image(H, bx_mm, by_mm)
            black_vals.append(read_ring_darkness(gray, bcx, bcy, true_r))

        ys.append(y_mm)
        whites.append(white_val)
        blacks.append(min(black_vals))

    white_coeffs = np.polyfit(ys, whites, degree)
    black_coeffs = np.polyfit(ys, blacks, degree)
    return white_coeffs, black_coeffs


# ---------------------------------------------------------------------
# Bubble reading: absolute scale (no more row-relative comparison, no
# more per-letter correction -- enabled by the empty circles).
# ---------------------------------------------------------------------

FILL_THRESHOLD = 0.21     # fallback value if no clean separation is found
UNCERTAIN_MARGIN = 0.075  # same


def find_adaptive_threshold(values, min_gap=0.08, margin_fraction=0.3):
    """Looks for the biggest gap in the sorted list of a sheet's
    normalized darkness values, to automatically separate empty bubbles
    from marked ones -- without assuming a particular marking style up
    front (shading, cross, light pencil...).

    Returns (threshold, margin) in the middle of the biggest gap found,
    with a margin proportional to that gap's width (a clean gap gives a
    wide margin -> few flags; a tight gap gives a narrow margin -> more
    flags). Returns None if no sufficient gap is found (entirely blank
    sheet, entirely marked, or image too degraded to cleanly separate)."""
    sorted_vals = sorted(values)
    best_gap, best_idx = 0.0, None
    for i in range(1, len(sorted_vals)):
        gap = sorted_vals[i] - sorted_vals[i - 1]
        if gap > best_gap:
            best_gap = gap
            best_idx = i
    if best_idx is None or best_gap < min_gap:
        return None
    threshold = (sorted_vals[best_idx - 1] + sorted_vals[best_idx]) / 2
    margin = best_gap * margin_fraction
    return threshold, margin, best_gap


def extract_identity_zone(img, H, cfg, px_per_mm=14):
    """Straightens (perspective) the 'Nom : ... Classe : ...' area into
    a clean rectangular image, from the homography already computed for
    this sheet. Useful to show the teacher who filled in a copy whose
    number doesn't match anyone in the roster."""
    z = identity_zone_corners_for(cfg)  # TL, TR, BR, BL
    src_img_pts = np.float32([mm_to_image(H, x, y) for (x, y) in z])

    width_mm = z[1][0] - z[0][0]
    height_mm = z[0][1] - z[3][1]
    out_w = max(1, int(round(width_mm * px_per_mm)))
    out_h = max(1, int(round(height_mm * px_per_mm)))
    dst_pts = np.float32([[0, 0], [out_w, 0], [out_w, out_h], [0, out_h]])

    M = cv2.getPerspectiveTransform(src_img_pts, dst_pts)
    return cv2.warpPerspective(img, M, (out_w, out_h))


def read_config_barcode(gray, H):
    """Decodes the configuration barcode (16 bars, FIXED position --
    independent of cfg, so readable even before knowing which
    configuration was used). Self-calibrates its own black/white
    threshold from the range of values observed on the 16 bars
    themselves (like a real barcode), falling back to the top-left
    corner marker if the 16 bars ever look too similar."""
    positions = config_barcode_bit_positions()
    raw_vals = []
    for (x_mm, y_mm) in positions:
        scale = local_scale_px_per_mm(H, x_mm, y_mm)
        cx, cy = mm_to_image(H, x_mm, y_mm)
        raw_vals.append(read_mean_gray(gray, cx, cy, 0.8 * scale))

    lo, hi = min(raw_vals), max(raw_vals)
    if hi - lo < 30:
        # Bars too similar to self-calibrate (unlikely case): fall back
        # to the TL marker's black and a white margin point.
        canon = corner_points()
        tl_x, tl_y = canon["TL"]
        scale = local_scale_px_per_mm(H, tl_x, tl_y)
        cx, cy = mm_to_image(H, tl_x, tl_y)
        black_ref = read_mean_gray(gray, cx, cy, 0.3 * scale)
        white_ref = hi
        threshold = (black_ref + white_ref) / 2
    else:
        threshold = (lo + hi) / 2

    bits = [1 if v < threshold else 0 for v in raw_vals]
    value = 0
    for b in bits:
        value = (value << 1) | b
    return value


def analyse(image_path, cfg=None, side=0, fill_threshold=None, uncertain_margin=None,
            adaptive=True, debug_out=None, illumination_degree=2,
            identity_crop_out=None):
    """
    cfg: SheetConfig used to generate the sheet. If not given (default),
    it's read automatically from the configuration barcode printed at
    the bottom of the sheet -- no need to know it or re-enter it by hand
    (front/back included).

    side: only useful if `cfg` is given by hand (0=front, 1=back);
    ignored if cfg=None, since then the side is read directly from the
    barcode.

    adaptive=True (default): threshold and margin are recomputed from
    THIS sheet's own values (biggest gap between light and dark
    bubbles) rather than using fixed constants -- naturally adapts to
    the marking style (shading, cross, light pencil, black pen...)
    without manual tuning per style. Pass adaptive=False to force
    fill_threshold/uncertain_margin (or their default values) instead.
    """
    img = cv2.imread(image_path)
    if img is None:
        raise FileNotFoundError(image_path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    candidates, hierarchy, _ = find_candidate_squares(gray)
    finders, plains = classify_marks(candidates, hierarchy)
    img_corners = identify_corners(finders, plains, gray.shape[::-1])
    H = build_homography(img_corners)
    check_resolution(H)

    config_parity_ok = None
    if cfg is None:
        value = read_config_barcode(gray, H)
        decoded, config_parity_ok = decode_config_value(value)
        side = decoded["side"]
        cfg = SheetConfig(
            n_questions=decoded["n_questions"],
            n_choices=decoded["n_choices"],
            scale=decoded["scale"],
            show_classe=decoded["show_classe"],
        )

    identity_crop = extract_identity_zone(img, H, cfg)
    if identity_crop_out:
        cv2.imwrite(identity_crop_out, identity_crop)

    white_coeffs, black_coeffs = fit_illumination_curve(gray, H, cfg, side=side, degree=illumination_degree)

    # --- Sheet number (8-bit pattern, top-right corner) ---
    bits = []
    for (x_mm, y_mm) in id_bit_cell_centers_for(cfg):
        scale = local_scale_px_per_mm(H, x_mm, y_mm)
        cx, cy = mm_to_image(H, x_mm, y_mm)
        raw = read_mean_gray(gray, cx, cy, 0.9 * scale)
        w = illumination_at(white_coeffs, y_mm)
        b = illumination_at(black_coeffs, y_mm)
        span = max(w - b, 1.0)
        normalized = (w - raw) / span
        bits.append(1 if normalized >= 0.5 else 0)
    sheet_number = 0
    for bit in bits:
        sheet_number = (sheet_number << 1) | bit

    # --- Bubbles: pass 1, raw measurement of every value ---
    centers = bubble_centers_for(cfg, side=side)
    _, n_side_count = side_question_range(cfg, side)
    bd = bubble_d_native(dataclasses.replace(cfg, n_questions=n_side_count))
    raw_values = {}
    px_positions = {}
    for q, pts_mm in centers.items():
        row_vals, row_px = [], []
        for (x_mm, y_mm) in pts_mm:
            scale = local_scale_px_per_mm(H, x_mm, y_mm)
            radius_px = (bd / 2) * scale * 0.85
            cx, cy = mm_to_image(H, x_mm, y_mm)
            raw = read_mean_gray(gray, cx, cy, radius_px)
            w = illumination_at(white_coeffs, y_mm)
            b = illumination_at(black_coeffs, y_mm)
            span = max(w - b, 1.0)
            normalized = (w - raw) / span  # 0 = local white, 1 = local black
            row_vals.append(normalized)
            row_px.append((cx, cy, radius_px))
        raw_values[q] = row_vals
        px_positions[q] = row_px

    # --- Correcting residual drift along the sheet ---
    # Even after per-row white/black calibration, a residual contrast
    # loss can remain on a large sheet (see the same phenomenon
    # encountered and fixed in v1). So each bubble is compared to the
    # minimum of ITS OWN row, which absorbs any drift still present at
    # that exact spot on the sheet.
    row_min = {q: min(vals) for q, vals in raw_values.items()}
    detrended = {q: [v - row_min[q] for v in vals] for q, vals in raw_values.items()}

    # --- Threshold and margin: GLOBAL (robust fallback, computed over
    # every bubble on the sheet) then, for EACH ROW, a LOCAL threshold
    # of its own if that row shows its own clean gap. A student who
    # presses harder on some answers than others (light pencil on one
    # question, heavy pen on another) shouldn't cause the more lightly
    # marked answers to be missed: the GLOBAL threshold, computed over
    # every bubble mixed together, would otherwise be pulled upward by
    # the darkest answers and miss the lighter ones (observed on a real
    # copy: Q1 shaded dark, Q2-Q8 in light gray, only Q1 detected). The
    # global threshold remains the fallback used for a row that has no
    # clean gap on its own (blank row, or marking too ambiguous even
    # locally).
    ROW_MIN_GAP = 0.05
    # If even a row's LEAST dark bubble already exceeds this value, no
    # bubble in that row looks like blank paper -- maybe ALL the bubbles
    # in that row are marked (4/4, 5/5...), or the area is smudged/
    # shaded. Re-basing against the row's minimum (see detrended) no
    # longer makes sense then (it would mistake the "least marked"
    # bubble for white): the row is flagged for manual review rather
    # than risk silently reading it as entirely blank.
    ROW_SUSPECT_MIN_RAW = 0.25
    row_thresholds = {}
    suspect_rows = set()

    if adaptive:
        all_vals = [v for row in detrended.values() for v in row]
        global_found = find_adaptive_threshold(all_vals)
        global_threshold, global_margin, gap_found = global_found if global_found else (None, None, None)

        any_threshold_found = global_found is not None
        for q, vals in detrended.items():
            if min(raw_values[q]) > ROW_SUSPECT_MIN_RAW:
                suspect_rows.add(q)
                row_thresholds[q] = None
                continue
            local_found = find_adaptive_threshold(vals, min_gap=ROW_MIN_GAP)
            if local_found is not None:
                row_thresholds[q] = (local_found[0], local_found[1])
                any_threshold_found = True
            elif global_found is not None:
                row_thresholds[q] = (global_threshold, global_margin)
            else:
                # Neither a local threshold (this row) nor a global one
                # (the whole sheet): no reliable separation, this row is
                # flagged entirely for manual correction.
                row_thresholds[q] = None
        sheet_needs_manual_review = not any_threshold_found
        used_threshold = global_threshold if global_threshold is not None else FILL_THRESHOLD
        used_margin = global_margin if global_margin is not None else UNCERTAIN_MARGIN
    else:
        gap_found = None
        sheet_needs_manual_review = False
        used_threshold = fill_threshold if fill_threshold is not None else FILL_THRESHOLD
        used_margin = uncertain_margin if uncertain_margin is not None else UNCERTAIN_MARGIN
        for q in detrended:
            row_thresholds[q] = (used_threshold, used_margin)

    # --- Bubbles: pass 2, decision with the chosen threshold (local to
    # the row when it exists, otherwise the global fallback) ---
    results = {}
    debug_img = img.copy() if debug_out else None

    for q, pts_mm in centers.items():
        row_result = {"answers": [], "flagged": [], "confidence": {}}
        row_vals_detrended = detrended[q]
        row_thresh = row_thresholds.get(q)
        row_is_suspect = q in suspect_rows
        for i in range(cfg.n_choices):
            letter = cfg.choices[i]
            row_result["confidence"][letter] = round(raw_values[q][i], 3)

            if row_is_suspect or row_thresh is None:
                # Suspect row (no reference white bubble) or no
                # reliable threshold: nothing is decided, it's flagged
                # for manual correction.
                row_result["flagged"].append(letter)
            else:
                row_threshold, row_margin = row_thresh
                dist_to_threshold = row_vals_detrended[i] - row_threshold
                if abs(dist_to_threshold) <= row_margin:
                    row_result["flagged"].append(letter)
                elif dist_to_threshold > 0:
                    row_result["answers"].append(letter)

            if debug_img is not None:
                cx, cy, radius_px = px_positions[q][i]
                if letter in row_result["flagged"]:
                    color = (0, 165, 255)  # orange: uncertain
                elif letter in row_result["answers"]:
                    color = (0, 200, 0)    # green: marked
                else:
                    color = (0, 0, 255)    # red: blank
                cv2.circle(debug_img, (int(cx), int(cy)), int(radius_px), color, 2)

        results[q] = row_result

    if debug_out:
        for name, (x, y) in img_corners.items():
            cv2.circle(debug_img, (int(x), int(y)), 8, (255, 0, 0), 2)
            cv2.putText(debug_img, name, (int(x) + 10, int(y)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)
        cv2.imwrite(debug_out, debug_img)

    return {
        "sheet_number": sheet_number,
        "side": side,
        "is_recto_verso": needs_recto_verso(cfg),
        "questions": results,
        "threshold_used": round(used_threshold, 3),
        "margin_used": round(used_margin, 3),
        "gap_found": round(gap_found, 3) if gap_found is not None else None,
        "sheet_needs_manual_review": sheet_needs_manual_review,
        "identity_crop": identity_crop,
        "config_used": cfg,
        "config_parity_ok": config_parity_ok,
    }


def format_report(result):
    """Small human-readable summary: detected answers + list of cases
    to check by hand. Output text intentionally stays in French (see
    module docstring)."""
    lines = [f"Feuille n\u00b0 {result['sheet_number']}"]
    if result.get("sheet_needs_manual_review"):
        lines.append("\u26a0\ufe0f AUCUNE SÉPARATION NETTE TROUVÉE — feuille entière à corriger à la main")
        lines.append("   (probablement un style de marquage non conforme à la consigne)")
        return "\n".join(lines)
    any_flag = False
    for q, r in result["questions"].items():
        ans = ",".join(r["answers"]) if r["answers"] else "\u2014"
        flag = f"  \u26a0 \u00e0 v\u00e9rifier : {', '.join(r['flagged'])}" if r["flagged"] else ""
        if r["flagged"]:
            any_flag = True
        lines.append(f"  Q{q:>2} : {ans}{flag}")
    if not any_flag:
        lines.append("(aucun cas incertain sur cette copie)")
    return "\n".join(lines)




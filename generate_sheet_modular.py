"""
Modular answer-sheet generator.

Reuses the v2 design (5 columns, empty circles, repeated headers,
numbered pattern) but makes EVERYTHING configurable:
- title / subtitle
- presence of the "Classe :" ("Class:") field
- number of questions (the layout recomputes itself automatically)
- number of answer columns (2 to 5)
- sheet scale (homothety of the base design, native = A6)
- number of sheets per printed page (rows x cols tiling)
- output page format (A4 by default)

The "native" design (scale 1) is exactly the already-validated A6
format (105 x 148.5 mm, 4.2mm bubbles). Any other scale is a pure
homothety of this design: same proportions, same alignment markers,
just bigger or smaller. Sheet-printed text (labels drawn on the answer
sheet itself, e.g. "Nom :"/"Classe :"/"Exemple :") is intentionally
left in French: it's the printed document's content, not the
software's interface (see translations.py for the interface)."""
from dataclasses import dataclass, replace as dataclass_replace
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm as MM

# --- Native dimensions (scale 1), from the validated A6 design ---
NATIVE_W = 105.0
NATIVE_H = 148.5

MARK_SIZE = 7.0
MARK_MARGIN = 6.5
SMALL_MARK_SIZE = 3.5

BUBBLE_D = 4.2
BUBBLE_GAP = 11.0
COL_Q_LABEL_W = 8.0
GRID_LEFT_LOCAL = 8.0
ROW_X_SHIFT = 10.0
ROW_H = 6.6                # native row height (12 questions, scale 1)
BUBBLE_RATIO = BUBBLE_D / ROW_H   # bubble/row ratio, kept at every scale

N_ID_BITS = 8
ID_BORDER = 1.0
ID_GAP = 0.5
ID_COLS, ID_ROWS = 4, 2

MIN_BUBBLE_MM = 3.15   # minimum printed bubble size for reliable detection
# (tuned so that 64 questions just fit double-sided on an A4 sheet: 32+32
# gives 3.186mm bubbles, a hair under the old round 3.2mm threshold)

CHOICES_MAX = ["A", "B", "C", "D", "E", "F"]  # up to 6 answer columns

# --- Configuration barcode (bottom of the sheet) ---
# Encodes n_questions, n_choices, scale and show_classe directly on the
# sheet: the reading script no longer needs to be told the configuration
# by hand, it reads it on its own.
BARCODE_N_BITS = 15   # 6 (total n_questions, 1-64) + 2 (n_choices) + 4 (scale) + 1 (classe) + 1 (side) + 1 (parity)
BARCODE_BAR_W = 1.5
BARCODE_BAR_GAP = 0.5
BARCODE_BAR_H = 4.0
BARCODE_Y_CENTER = 10.0   # mm, within the vertical band of the bottom markers

SQRT2 = 2 ** 0.5

MAX_QUESTIONS = 64  # beyond that, no practical use -> fits on 6 bits (1-64)
MAX_SCALE = 2.0      # maximum answer-sheet size: A4 (= A6 x2)

# --- Labels printed ON the sheet itself (Nom/Classe/Exemple/Recto-Verso),
# separate from the application's own interface translations (see
# translations.py): a teacher can print sheets in a different language
# than the one they use the app in, though qcm_app.py normally passes
# its own current interface language here so both match by default.
# Purely cosmetic text -- reading (detect_modular.py) never looks at it,
# only at the fixed geometry of the identity zone and bubble positions,
# so any language here is always safe to read back.
SHEET_LABELS = {
    "fr": {
        "nom": "Nom :",
        "classe": "Classe :",
        "exemple": "Exemple :",
        "reponse_sing": "réponse",
        "reponse_plur": "réponses",
        "et": " et ",
        "recto": "Recto (1/2)",
        "verso": "Verso (2/2)",
        "numero_prefix": "n° ",
    },
    "en": {
        "nom": "Name:",
        "classe": "Class:",
        "exemple": "Example:",
        "reponse_sing": "answer",
        "reponse_plur": "answers",
        "et": " and ",
        "recto": "Front (1/2)",
        "verso": "Back (2/2)",
        "numero_prefix": "No. ",
    },
    "de": {
        "nom": "Name:",
        "classe": "Klasse:",
        "exemple": "Beispiel:",
        "reponse_sing": "Antwort",
        "reponse_plur": "Antworten",
        "et": " und ",
        "recto": "Vorderseite (1/2)",
        "verso": "Rückseite (2/2)",
        "numero_prefix": "Nr. ",
    },
    "es": {
        "nom": "Nombre:",
        "classe": "Clase:",
        "exemple": "Ejemplo:",
        "reponse_sing": "respuesta",
        "reponse_plur": "respuestas",
        "et": " y ",
        "recto": "Anverso (1/2)",
        "verso": "Reverso (2/2)",
        "numero_prefix": "n.º ",
    },
}


def sheet_labels(lang):
    return SHEET_LABELS.get(lang, SHEET_LABELS["fr"])


def needs_recto_verso(cfg):
    """True if the number of questions doesn't fit on a single side at
    the requested scale (within the MAX_SCALE = A4 limit)."""
    return cfg.n_questions > max_questions_for_scale(min(cfg.scale, MAX_SCALE))


def side_question_range(cfg, side):
    """(1st question's number, number of questions) for side 'side'
    (0 = front, 1 = back) of cfg. If double-sided isn't needed, side=0
    returns everything, side=1 is empty (0 questions)."""
    total = cfg.n_questions
    if not needs_recto_verso(cfg):
        return (1, total) if side == 0 else (total + 1, 0)
    n1 = (total + 1) // 2
    n2 = total - n1
    return (1, n1) if side == 0 else (n1 + 1, n2)


def encode_config(cfg, side=0):
    """cfg -> a 15-bit integer.
    [n_questions_total-1 (6b)][n_choices-3 (2b)][scale_level (4b)]
    [show_classe (1b)][side: 0=front/1=back (1b)][parity (1b)]."""
    if not (1 <= cfg.n_questions <= MAX_QUESTIONS):
        raise ValueError(f"n_questions doit \u00eatre entre 1 et {MAX_QUESTIONS} pour le code-barres.")
    n_q_code = cfg.n_questions - 1              # 0-63 (6 bits)
    n_choices_code = cfg.n_choices - 3          # 0-3 (for 3 to 6 answers)
    scale_level = round(math_log2(cfg.scale) * 2) + 8  # sqrt(2) step, offset 8
    scale_level = max(0, min(15, scale_level))
    show_classe_bit = 1 if cfg.show_classe else 0
    side_bit = 1 if side else 0

    body = (n_q_code << 8) | (n_choices_code << 6) | (scale_level << 2) | (show_classe_bit << 1) | side_bit
    parity_bit = bin(body).count("1") % 2
    return (body << 1) | parity_bit


def decode_config_value(value):
    """15-bit integer -> (parameter dict + 'side', parity_ok: bool).
    'n_questions' in the dict is the TOTAL (both sides added together if
    double-sided); 'side' tells which side was read (0=front, 1=back)."""
    parity_bit = value & 1
    body = value >> 1
    expected_parity = bin(body).count("1") % 2
    parity_ok = (parity_bit == expected_parity)

    side = body & 1
    show_classe = bool((body >> 1) & 1)
    scale_level = (body >> 2) & 0b1111
    n_choices = ((body >> 6) & 0b11) + 3
    n_questions = ((body >> 8) & 0b111111) + 1
    scale = SQRT2 ** (scale_level - 8)
    return {
        "n_questions": n_questions,
        "n_choices": n_choices,
        "scale": scale,
        "show_classe": show_classe,
        "side": side,
    }, parity_ok


def math_log2(x):
    import math
    return math.log(x, 2)


def config_barcode_bit_positions():
    """14 native (x_mm, y_mm) positions of the barcode's bars,
    independent of cfg (FIXED position, readable even before knowing
    the sheet's configuration)."""
    total_w = BARCODE_N_BITS * BARCODE_BAR_W + (BARCODE_N_BITS - 1) * BARCODE_BAR_GAP
    x0 = (NATIVE_W - total_w) / 2
    positions = []
    for k in range(BARCODE_N_BITS):
        cx = x0 + k * (BARCODE_BAR_W + BARCODE_BAR_GAP) + BARCODE_BAR_W / 2
        positions.append((cx, BARCODE_Y_CENTER))
    return positions


@dataclass
class SheetConfig:
    title: str = "Feuille-réponse"
    subtitle: str = "QCM Physique-Chimie \u2013 M. GIRARD"
    n_questions: int = 12
    n_choices: int = 5          # number of answer columns (3 to 6)
    show_classe: bool = True
    scale: float = 1.0          # homothety: 1.0 = native A6 format
    tiles_rows: int = 2
    tiles_cols: int = 2
    page_w: float = 210.0       # output page (mm); A4 by default
    page_h: float = 297.0
    lang: str = "fr"            # language of the text PRINTED on the sheet (see SHEET_LABELS)

    @property
    def choices(self):
        return CHOICES_MAX[: self.n_choices]

    @property
    def sheet_w(self):
        return NATIVE_W * self.scale

    @property
    def sheet_h(self):
        return NATIVE_H * self.scale

    def resolve_page_size(self):
        """Returns (page_w, page_h) to actually use: as-is if the tiling
        fits in this orientation, otherwise the page rotated 90° if it
        fits that way. Raises an error if neither orientation works."""
        fit_w = self.tiles_cols * self.sheet_w
        fit_h = self.tiles_rows * self.sheet_h
        if fit_w <= self.page_w + 0.5 and fit_h <= self.page_h + 0.5:
            return self.page_w, self.page_h
        if fit_w <= self.page_h + 0.5 and fit_h <= self.page_w + 0.5:
            return self.page_h, self.page_w  # page rotated 90°
        raise ValueError(
            f"{self.tiles_rows}x{self.tiles_cols} feuilles de "
            f"{self.sheet_w:.0f}x{self.sheet_h:.0f}mm ne tiennent pas sur une page "
            f"{self.page_w:.0f}x{self.page_h:.0f}mm, même tournée à 90\u00b0."
        )

    def validate(self):
        errors = []
        if not (3 <= self.n_choices <= 6):
            errors.append("n_choices doit être entre 3 et 6.")
        if self.n_questions < 1:
            errors.append("n_questions doit être au moins 1.")
        if self.n_questions > MAX_QUESTIONS:
            errors.append(f"n_questions ne peut pas dépasser {MAX_QUESTIONS}.")
        if self.scale > MAX_SCALE + 1e-9:
            errors.append(
                f"scale={self.scale:.3f} dépasse la taille de feuille maximale autorisée "
                f"(A4, scale={MAX_SCALE}). Utilise le recto-verso plutôt qu'une feuille plus grande."
            )
        try:
            self.resolve_page_size()
        except ValueError as e:
            errors.append(str(e))

        # Bubble size: checked per SIDE (not on the total), since
        # double-sided printing splits the questions across 2 pages if needed.
        eff_scale = min(self.scale, MAX_SCALE)
        n1, _ = side_question_range(self, 0)
        _, n2 = side_question_range(self, 1)
        for n_side in ([n1] if n2 == 0 else [n1, n2]):
            printed_bubble = BUBBLE_D * row_h_native(n_side) / ROW_H * eff_scale
            if printed_bubble < MIN_BUBBLE_MM - 1e-6:
                max_n = max_questions_for_scale(eff_scale)
                errors.append(
                    f"Avec {self.n_questions} questions au total à l'échelle {self.scale:.2f} "
                    f"(recto-verso -> {n_side} sur cette face), les bulles imprimées feraient "
                    f"{printed_bubble:.1f}mm (minimum fiable : {MIN_BUBBLE_MM}mm). "
                    f"Maximum par face à cette échelle : {max_n} (donc {2*max_n} au total en recto-verso)."
                )
                break
        if errors:
            raise ValueError(" / ".join(errors))


# ---------------------------------------------------------------------
# Dynamic layout (depends only on n_questions; the rest of the "chrome"
# -- title, identity, example, headers -- has a fixed height).
# ---------------------------------------------------------------------

TITLE_Y = NATIVE_H - 11.0
SUBTITLE_Y = NATIVE_H - 16.0
IDENTITY_Y = NATIVE_H - 24.0
EXAMPLE_Y = NATIVE_H - 32.0
EXAMPLE_DIVIDER_Y = NATIVE_H - 36.0
GRID_TOP = NATIVE_H - 45.5     # y of question 1
BOTTOM_MARGIN = MARK_MARGIN + MARK_SIZE + 2.0   # safety margin below the grid

HEADER_TEXT_OFFSET = 2.6   # header text baseline offset below its reference point
HEADER_CLEARANCE = 1.5     # minimum visual margin between header text and the next circle's top
MID_GAP_AFTER = 2.5     # space between the 1st block's last question and the divider line
SPLIT_THRESHOLD = 7     # beyond this many questions, split into 2 blocks with a recap


MIN_TEXT_SHRINK_RATIO = 0.65  # text never shrinks below 65% of its nominal size


def text_shrink_ratio(cfg):
    """Text (numbers, headers) shrink factor caused by rows tightening
    when there are many questions, CAPPED at a readable minimum
    (MIN_TEXT_SHRINK_RATIO) rather than following bubble size forever."""
    ratio = row_h_native(cfg.n_questions) / ROW_H
    return max(MIN_TEXT_SHRINK_RATIO, min(1.0, ratio))


def header_gap(cfg):
    """Vertical space (mm, native) needed between a header row's
    reference position and the following row of bubbles, so the text
    never touches the circles. Uses the SAME shrink factor as the text
    itself (with its readability floor) rather than just bubble size,
    otherwise "capped" text (so sometimes bigger than the bubble) could
    overflow."""
    effective_half_bubble = (BUBBLE_D / 2) * text_shrink_ratio(cfg)
    return max(bubble_d_native(cfg) / 2, effective_half_bubble) + HEADER_TEXT_OFFSET + HEADER_CLEARANCE


def available_grid_height():
    return GRID_TOP - BOTTOM_MARGIN


def split_layout(n_questions):
    """Returns (n1, n2): number of questions in the 1st and 2nd block.
    n2=0 if there's no split (few questions, one block is enough)."""
    if n_questions <= SPLIT_THRESHOLD:
        return n_questions, 0
    n1 = (n_questions + 1) // 2
    return n1, n_questions - n1


def row_h_native(n_questions):
    """Native row height (mm, scale 1) so n_questions fit in the
    available space, with or without splitting into 2 blocks. For the
    split case, the space taken by the recap header depends on bubble
    size, which itself depends on row_h -> solved by a few fixed-point
    iterations (converges very quickly)."""
    n1, n2 = split_layout(n_questions)
    avail = available_grid_height()
    if n2 == 0:
        return avail / n1
    row_h = avail / n_questions  # initial estimate (without the header overhead)
    for _ in range(5):
        bd = BUBBLE_RATIO * row_h
        overhead = MID_GAP_AFTER + (bd / 2 + HEADER_TEXT_OFFSET + HEADER_CLEARANCE)
        row_h = (avail - overhead) / n_questions
    return row_h


def max_questions_for_scale(scale, min_bubble_mm=MIN_BUBBLE_MM):
    """Maximum number of questions fitting at this scale while keeping
    printed bubbles of at least `min_bubble_mm`."""
    avail = available_grid_height()
    min_row_h_native = min_bubble_mm / (BUBBLE_RATIO * scale)
    n_no_split = int(avail / min_row_h_native)
    if n_no_split <= SPLIT_THRESHOLD:
        return max(1, n_no_split)
    overhead = MID_GAP_AFTER + (min_row_h_native * BUBBLE_RATIO / 2 + HEADER_TEXT_OFFSET + HEADER_CLEARANCE)
    n_split = int((avail - overhead) / min_row_h_native)
    return max(1, n_split)


# ---------------------------------------------------------------------
# Drawing (every function receives `s` = the sheet's scale; a "native"
# mm coordinate x becomes x*s*MM at drawing time).
# ---------------------------------------------------------------------

def draw_finder_pattern(c, x, y, size):
    module = size / 7.0
    c.setFillColorRGB(0, 0, 0)
    c.rect(x, y, size, size, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.rect(x + module, y + module, size - 2 * module, size - 2 * module, fill=1, stroke=0)
    c.setFillColorRGB(0, 0, 0)
    c.rect(x + 2 * module, y + 2 * module, size - 4 * module, size - 4 * module, fill=1, stroke=0)


def bit_grid_geometry(size_pts, s):
    border = ID_BORDER * s * MM
    gap = ID_GAP * s * MM
    inner = size_pts - 2 * (border + gap)
    zone_x0 = border + gap
    zone_y0 = border + gap
    cell_w = inner / ID_COLS
    cell_h = inner / ID_ROWS
    return zone_x0, zone_y0, cell_w, cell_h, border


def draw_id_pattern(c, x, y, size_pts, s, number):
    if not (1 <= number <= 255):
        raise ValueError("number doit \u00eatre entre 1 et 255")
    bits = [(number >> (N_ID_BITS - 1 - k)) & 1 for k in range(N_ID_BITS)]
    zone_x0, zone_y0, cell_w, cell_h, border = bit_grid_geometry(size_pts, s)
    c.setFillColorRGB(0, 0, 0)
    c.rect(x, y, size_pts, size_pts, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.rect(x + border, y + border, size_pts - 2 * border, size_pts - 2 * border, fill=1, stroke=0)
    for k, bit in enumerate(bits):
        row = k // ID_COLS
        col = k % ID_COLS
        cell_x = x + zone_x0 + col * cell_w
        cell_y = y + zone_y0 + (ID_ROWS - 1 - row) * cell_h
        if bit:
            c.setFillColorRGB(0, 0, 0)
            c.rect(cell_x, cell_y, cell_w, cell_h, fill=1, stroke=0)


def draw_config_barcode(c, ox, oy, s, cfg, side=0):
    """Draws the configuration barcode: 15 thin vertical bars (never
    confused with a square marker), positioned between the sheet's two
    bottom markers."""
    value = encode_config(cfg, side=side)
    bits = [(value >> (BARCODE_N_BITS - 1 - k)) & 1 for k in range(BARCODE_N_BITS)]

    total_w = BARCODE_N_BITS * BARCODE_BAR_W + (BARCODE_N_BITS - 1) * BARCODE_BAR_GAP
    x0 = (NATIVE_W - total_w) / 2
    y_center = BARCODE_Y_CENTER

    for k, bit in enumerate(bits):
        x = x0 + k * (BARCODE_BAR_W + BARCODE_BAR_GAP)
        bar_x = ox + x * s * MM
        bar_y = oy + (y_center - BARCODE_BAR_H / 2) * s * MM
        bar_w = BARCODE_BAR_W * s * MM
        bar_h = BARCODE_BAR_H * s * MM
        # Always draw an outline, filled only if bit=1: this keeps every
        # position identifiable even at 0, useful for future fine
        # alignment if needed.
        c.setLineWidth(max(0.3, 0.4 * s))
        c.setFillColorRGB(0, 0, 0) if bit else c.setFillColorRGB(1, 1, 1)
        c.rect(bar_x, bar_y, bar_w, bar_h, fill=1, stroke=1)
    c.setFillColorRGB(0, 0, 0)


def draw_corner_marks(c, ox, oy, s, sheet_number=None, number_prefix="n\u00b0 "):
    size = MARK_SIZE * s * MM
    margin = MARK_MARGIN * s * MM
    small = SMALL_MARK_SIZE * s * MM
    w, h = NATIVE_W * s * MM, NATIVE_H * s * MM

    c.setFillColorRGB(0, 0, 0)
    draw_finder_pattern(c, ox + margin, oy + h - margin - size, size)
    mark_x = ox + w - margin - size
    mark_y = oy + h - margin - size
    if sheet_number is not None:
        draw_id_pattern(c, mark_x, mark_y, size, s, sheet_number)
        label = f"{number_prefix}{sheet_number}"
        font_size = 8.0 * s
        while c.stringWidth(label, "Helvetica-Bold", font_size) > size and font_size > 3:
            font_size -= 0.3
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", font_size)
        c.drawCentredString(mark_x + size / 2, mark_y - 4 * s * MM, label)
    else:
        draw_finder_pattern(c, mark_x, mark_y, size)
    draw_finder_pattern(c, ox + margin, oy + margin, size)
    c.setFillColorRGB(0, 0, 0)
    c.rect(ox + w - margin - small, oy + margin, small, small, fill=1, stroke=0)


def row_x_shift(cfg):
    """Horizontal shift (mm, native) of the number+bubbles block,
    computed so this block is centered across the sheet's width,
    regardless of the number of answer columns (3 to 6)."""
    bd = bubble_d_native(cfg)
    content_left_local = GRID_LEFT_LOCAL - 2.0   # same reference as label_left in draw_bubble_row
    content_right_local = GRID_LEFT_LOCAL + COL_Q_LABEL_W + (cfg.n_choices - 1) * BUBBLE_GAP + bd
    content_w = content_right_local - content_left_local
    margin = 6.0   # same margin as the rest of the sheet's lines/rules
    usable_w = NATIVE_W - 2 * margin
    desired_left = margin + (usable_w - content_w) / 2
    return desired_left - content_left_local


def bubble_x(i, cfg):
    """LOCAL x position (mm, before applying the row_x_shift centering
    offset) of column i."""
    return GRID_LEFT_LOCAL + COL_Q_LABEL_W + i * BUBBLE_GAP + bubble_d_native(cfg) / 2


def bubble_d_native(cfg):
    """Native bubble diameter (mm, scale 1), depending on the number of
    questions: row height varies with n_questions (see row_h_native),
    and bubble diameter follows in the same ratio -- more questions ->
    thinner rows -> smaller bubbles (but never below
    MIN_BUBBLE_MM/scale, see validate())."""
    return BUBBLE_RATIO * row_h_native(cfg.n_questions)


ROW_SHADE_COLOR = (0.90, 0.90, 0.90)


def draw_row_shade(c, ox, oy, s, row_y_local, row_h, cfg):
    row_x = ox + row_x_shift(cfg) * s * MM
    x0 = row_x + (GRID_LEFT_LOCAL - 4) * s * MM
    x1 = row_x + (bubble_x(cfg.n_choices - 1, cfg) + bubble_d_native(cfg) / 2 + 1.5) * s * MM
    y0 = oy + (row_y_local - row_h / 2) * s * MM
    c.setFillColorRGB(*ROW_SHADE_COLOR)
    c.rect(x0, y0, x1 - x0, row_h * s * MM, fill=1, stroke=0)
    c.setFillColorRGB(0, 0, 0)


def text_scale(cfg):
    """Effective scale for text (numbers, column headers): the sheet's
    overall scale, reduced if rows are thinner than the 12-question
    reference -- but never below a readable floor (see
    text_shrink_ratio). Never grows beyond the normal scale when rows
    are more generous (few questions): only tightening is corrected."""
    return cfg.scale * text_shrink_ratio(cfg)


def draw_column_headers(c, ox, oy, s, row_y_local, cfg):
    row_x = ox + row_x_shift(cfg) * s * MM
    row_y = oy + row_y_local * s * MM
    ts = text_scale(cfg)
    c.setFont("Helvetica-Bold", 7.5 * ts)
    c.setFillColorRGB(0.25, 0.25, 0.25)
    for i, letter in enumerate(cfg.choices):
        cx = row_x + bubble_x(i, cfg) * s * MM
        c.drawCentredString(cx, row_y - 2.6 * ts * MM, letter)
    c.setFillColorRGB(0, 0, 0)


def draw_bubble_row(c, ox, oy, s, row_y_local, label, cfg, filled_indices=None,
                     label_font_size=8, center_shift_mm=0):
    filled_indices = filled_indices or []
    row_x = ox + row_x_shift(cfg) * s * MM
    row_y = oy + row_y_local * s * MM
    label_left = row_x + (GRID_LEFT_LOCAL - 2) * s * MM
    label_right_limit = row_x + (bubble_x(0, cfg) - bubble_d_native(cfg) / 2 - 1) * s * MM
    label_center_x = (label_left + label_right_limit) / 2 + center_shift_mm * s * MM
    available_w = label_right_limit - label_left
    size = label_font_size * text_scale(cfg)
    while c.stringWidth(label, "Helvetica-Bold", size) > available_w and size > 3:
        size -= 0.3
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", size)
    c.drawCentredString(label_center_x, row_y - size * 0.35, label)

    last_cx = row_x
    bd = bubble_d_native(cfg) * s * MM / 2
    for i in range(cfg.n_choices):
        cx = row_x + bubble_x(i, cfg) * s * MM
        cy = row_y
        last_cx = cx
        c.setLineWidth(max(0.4, s))
        color = (0, 0, 0) if i in filled_indices else (1, 1, 1)
        c.setFillColorRGB(*color)
        c.circle(cx, cy, bd, fill=1, stroke=1)
    c.setFillColorRGB(0, 0, 0)
    return last_cx + bd


def draw_shrinking_label(c, ox, x0_local, max_w_local, y_id, s, text, base_size=8.0):
    """Draws a left-aligned label (e.g. the "Nom :"/"Classe :" prompts),
    shrinking the font if needed so it never overflows into the
    following field -- translated labels (see SHEET_LABELS) aren't all
    the same length as the French originals this layout was tuned for
    (e.g. Spanish "Nombre:" vs French "Nom :"), so this guards against
    overlap regardless of language."""
    size = base_size * s
    max_w = max_w_local * s * MM
    while c.stringWidth(text, "Helvetica", size) > max_w and size > 4:
        size -= 0.3
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica", size)
    c.drawString(ox + x0_local * s * MM, y_id, text)


def draw_identity_field(c, ox, y_id, s, x0_local, x1_local, text=None):
    """Draws the identity field (Nom/"Name" or Classe/"Class") between
    local x0_local/x1_local abscissas (mm, native): either a line to
    fill in by hand (default behavior, `text=None`), or `text` printed
    directly on it (name/class already known from a CSV) -- in that
    case the line is omitted, the text replaces it."""
    x0 = ox + x0_local * s * MM
    x1 = ox + x1_local * s * MM
    if not text:
        c.line(x0, y_id - 1, x1, y_id - 1)
        return
    available_w = (x1 - x0) - 2 * s * MM
    size = 8 * s
    while c.stringWidth(text, "Helvetica-Bold", size) > available_w and size > 4:
        size -= 0.3
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", size)
    c.drawString(x0 + 1 * s * MM, y_id, text)


def draw_sheet(c, ox, oy, cfg, sheet_number=None, side=0, nom=None, classe=None):
    s = cfg.scale
    labels = sheet_labels(cfg.lang)
    draw_corner_marks(c, ox, oy, s, sheet_number=sheet_number, number_prefix=labels["numero_prefix"])
    draw_config_barcode(c, ox, oy, s, cfg, side=side)

    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 11 * s)
    c.drawCentredString(ox + (NATIVE_W * s / 2) * MM, oy + TITLE_Y * s * MM, cfg.title)
    c.setFont("Helvetica", 8 * s)
    c.drawCentredString(ox + (NATIVE_W * s / 2) * MM, oy + SUBTITLE_Y * s * MM, cfg.subtitle)

    # --- Identity --- (name/class printed directly if provided, else a
    # line to fill in by hand, see draw_identity_field)
    y_id = oy + IDENTITY_Y * s * MM
    draw_shrinking_label(c, ox, 6, 9, y_id, s, labels["nom"])
    if cfg.show_classe:
        draw_identity_field(c, ox, y_id, s, 16, 63, nom)
        draw_shrinking_label(c, ox, 66, 12, y_id, s, labels["classe"])
        draw_identity_field(c, ox, y_id, s, 79, 90, classe)
    else:
        draw_identity_field(c, ox, y_id, s, 16, 90, nom)
    c.setFillColorRGB(0, 0, 0)

    # --- Config adjusted for this side (same engine, n_questions = this side's count) ---
    start_q, n_side = side_question_range(cfg, side)
    cfg_side = dataclass_replace(cfg, n_questions=n_side)

    # --- Example --- (bubbles the same size as the questions -> cfg_side)
    example_filled = [1, 3] if cfg.n_choices >= 4 else [cfg.n_choices - 1]
    ex_letters = [cfg.choices[i] for i in example_filled]
    right_edge = draw_bubble_row(c, ox, oy, s, EXAMPLE_Y, labels["exemple"], cfg_side,
                                  filled_indices=example_filled,
                                  label_font_size=7, center_shift_mm=-3)
    caption_x = right_edge + 2 * s * MM
    max_w = ox + (NATIVE_W - 2) * s * MM - caption_x
    prefix = f"= {labels['reponse_plur'] if len(ex_letters) > 1 else labels['reponse_sing']}"
    letters_part = labels["et"].join(ex_letters)
    full_text = f"{prefix} {letters_part}"

    font_sz = 7 * text_scale(cfg_side)
    while c.stringWidth(full_text, "Helvetica-Oblique", font_sz) > max_w and font_sz > 5.5 * text_scale(cfg_side):
        font_sz -= 0.3
    c.setFont("Helvetica-Oblique", font_sz)
    if c.stringWidth(full_text, "Helvetica-Oblique", font_sz) > max_w:
        # Still not enough room on one line: wrap to a new line after
        # "réponse(s)" rather than cropping the text or its size. Both
        # lines are centered between caption_x and the right edge.
        line_center_x = caption_x + max_w / 2
        c.drawCentredString(line_center_x, oy + (EXAMPLE_Y + 0.6) * s * MM, prefix)
        c.drawCentredString(line_center_x, oy + (EXAMPLE_Y - 2.2) * s * MM, letters_part)
    else:
        c.drawString(caption_x, oy + (EXAMPLE_Y - 0.8) * s * MM, full_text)

    c.setLineWidth(max(0.3, 0.5 * s))
    c.setStrokeColorRGB(0.6, 0.6, 0.6)
    c.line(ox + 6 * s * MM, oy + EXAMPLE_DIVIDER_Y * s * MM,
           ox + (NATIVE_W - 6) * s * MM, oy + EXAMPLE_DIVIDER_Y * s * MM)
    c.setStrokeColorRGB(0, 0, 0)

    # --- Question grid (variable count, split into 2 if needed) ---
    n1, n2 = split_layout(n_side)
    row_h = row_h_native(n_side)
    gap = header_gap(cfg_side)

    draw_column_headers(c, ox, oy, s, GRID_TOP + gap, cfg_side)
    grid_top = GRID_TOP
    for q in range(n1):
        row_y = grid_top - q * row_h
        qn = start_q + q
        if (q + 1) % 2 == 0:
            draw_row_shade(c, ox, oy, s, row_y, row_h, cfg_side)
        draw_bubble_row(c, ox, oy, s, row_y, str(qn), cfg_side)

    if n2 > 0:
        mid_y = grid_top - n1 * row_h - MID_GAP_AFTER
        c.setLineWidth(max(0.3, 0.4 * s))
        c.setStrokeColorRGB(0.7, 0.7, 0.7)
        c.line(ox + 6 * s * MM, oy + (mid_y + gap * 0.6) * s * MM,
               ox + (NATIVE_W - 6) * s * MM, oy + (mid_y + gap * 0.6) * s * MM)
        c.setStrokeColorRGB(0, 0, 0)
        draw_column_headers(c, ox, oy, s, mid_y, cfg_side)

        grid_top2 = mid_y - gap
        for q in range(n2):
            row_y = grid_top2 - q * row_h
            qn = start_q + n1 + q
            if (n1 + q + 1) % 2 == 0:
                draw_row_shade(c, ox, oy, s, row_y, row_h, cfg_side)
            draw_bubble_row(c, ox, oy, s, row_y, str(qn), cfg_side)

    if needs_recto_verso(cfg):
        c.setFont("Helvetica-Oblique", 6.5 * s)
        c.setFillColorRGB(0.4, 0.4, 0.4)
        side_label = labels["recto"] if side == 0 else labels["verso"]
        c.drawCentredString(ox + (NATIVE_W * s / 2) * MM, oy + 3 * s * MM, side_label)
        c.setFillColorRGB(0, 0, 0)


def build(path, cfg, numbers=None):
    """numbers: list of numbers (1-255), one per tile on the page
    (length tiles_rows*tiles_cols), or None for plain markers."""
    cfg.validate()
    page_w, page_h = cfg.resolve_page_size()
    c = canvas.Canvas(path, pagesize=(page_w * MM, page_h * MM))
    n_tiles = cfg.tiles_rows * cfg.tiles_cols
    rv = needs_recto_verso(cfg)

    def draw_tile_page(side):
        for idx in range(n_tiles):
            row = idx // cfg.tiles_cols
            col = idx % cfg.tiles_cols
            ox = col * cfg.sheet_w * MM
            oy = (cfg.tiles_rows - 1 - row) * cfg.sheet_h * MM
            num = numbers[idx] if numbers else None
            draw_sheet(c, ox, oy, cfg, sheet_number=num, side=side)
        c.setDash(3, 3)
        c.setStrokeColorRGB(0.5, 0.5, 0.5)
        c.setLineWidth(0.5)
        for col in range(1, cfg.tiles_cols):
            x = col * cfg.sheet_w * MM
            c.line(x, 0, x, page_h * MM)
        for row in range(1, cfg.tiles_rows):
            y = row * cfg.sheet_h * MM
            c.line(0, y, page_w * MM, y)
        c.showPage()

    draw_tile_page(side=0)
    if rv:
        draw_tile_page(side=1)
    c.save()


def build_batch(path, cfg, start_number=1, n_sheets=None, identities=None):
    """Generates a multi-page PDF, one tile = one numbered sheet,
    consecutive numbers starting from start_number. n_sheets defaults to
    tiles_rows*tiles_cols (a single page).

    identities: {numero (int): (nom, classe)} optional -- if provided
    for a sheet, its name and/or class (either can be None) are printed
    directly on the sheet instead of the line to fill in by hand (see
    draw_identity_field).

    If the number of questions doesn't fit on one side (double-sided
    needed), each tile page is immediately followed by its matching
    back page (same numbers, 2nd half of the questions) -- works
    natively with automatic double-sided printing as long as there's 1
    sheet per page (cfg.tiles_rows=tiles_cols=1, the normal case for a
    sheet already at the maximum A4 size)."""
    cfg.validate()
    page_w, page_h = cfg.resolve_page_size()
    n_tiles = cfg.tiles_rows * cfg.tiles_cols
    if n_sheets is None:
        n_sheets = n_tiles
    rv = needs_recto_verso(cfg)
    if rv and n_tiles > 1:
        import warnings
        warnings.warn(
            "Recto-verso avec plusieurs feuilles par page : l'alignement "
            "recto/verso à l'impression duplex n'est pas garanti pour ce "
            "cas (non testé). Préfère tiles_rows=tiles_cols=1 pour un "
            "recto-verso fiable."
        )
    c = canvas.Canvas(path, pagesize=(page_w * MM, page_h * MM))
    numbers = list(range(start_number, start_number + n_sheets))

    def draw_tile_page(page_numbers, side):
        for idx, num in enumerate(page_numbers):
            row = idx // cfg.tiles_cols
            col = idx % cfg.tiles_cols
            ox = col * cfg.sheet_w * MM
            oy = (cfg.tiles_rows - 1 - row) * cfg.sheet_h * MM
            nom, classe = (identities.get(num) or (None, None)) if identities else (None, None)
            draw_sheet(c, ox, oy, cfg, sheet_number=num, side=side, nom=nom, classe=classe)
        c.setDash(3, 3)
        c.setStrokeColorRGB(0.5, 0.5, 0.5)
        c.setLineWidth(0.5)
        for col in range(1, cfg.tiles_cols):
            x = col * cfg.sheet_w * MM
            c.line(x, 0, x, page_h * MM)
        for row in range(1, cfg.tiles_rows):
            y = row * cfg.sheet_h * MM
            c.line(0, y, page_w * MM, y)
        c.showPage()

    for page_start in range(0, len(numbers), n_tiles):
        page_numbers = numbers[page_start: page_start + n_tiles]
        draw_tile_page(page_numbers, side=0)
        if rv:
            draw_tile_page(page_numbers, side=1)
    c.save()
    return numbers


# ---------------------------------------------------------------------
# Canonical coordinates (mm, scale 1) for the READING script. Use
# exactly the same functions as drawing (bubble_x, row_x_shift,
# row_h_native, header_gap...): generator and reader therefore can no
# longer diverge, unlike before when sheet_layout_v2.py kept its own
# constants separately.
# ---------------------------------------------------------------------

def corner_points_for(cfg):
    return {
        "TL": (MARK_MARGIN + MARK_SIZE / 2, NATIVE_H - MARK_MARGIN - MARK_SIZE / 2),
        "TR": (NATIVE_W - MARK_MARGIN - MARK_SIZE / 2, NATIVE_H - MARK_MARGIN - MARK_SIZE / 2),
        "BL": (MARK_MARGIN + MARK_SIZE / 2, MARK_MARGIN + MARK_SIZE / 2),
        "BR": (NATIVE_W - MARK_MARGIN - SMALL_MARK_SIZE / 2, MARK_MARGIN + SMALL_MARK_SIZE / 2),
    }


def bubble_centers_for(cfg, side=0):
    """{GLOBAL question number (per side_question_range): [(x_mm, y_mm) per column]}."""
    start_q, n_side = side_question_range(cfg, side)
    cfg_side = dataclass_replace(cfg, n_questions=n_side)
    n1, n2 = split_layout(n_side)
    row_h = row_h_native(n_side)
    gap = header_gap(cfg_side)
    shift = row_x_shift(cfg_side)
    result = {}
    for q in range(n1):
        y = GRID_TOP - q * row_h
        result[start_q + q] = [(shift + bubble_x(i, cfg_side), y) for i in range(cfg.n_choices)]
    if n2 > 0:
        mid_y = GRID_TOP - n1 * row_h - MID_GAP_AFTER
        grid_top2 = mid_y - gap
        for q in range(n2):
            y = grid_top2 - q * row_h
            result[start_q + n1 + q] = [(shift + bubble_x(i, cfg_side), y) for i in range(cfg.n_choices)]
    return result


def example_bubble_centers_for(cfg, side=0):
    _, n_side = side_question_range(cfg, side)
    cfg_side = dataclass_replace(cfg, n_questions=n_side)
    shift = row_x_shift(cfg_side)
    return [(shift + bubble_x(i, cfg_side), EXAMPLE_Y) for i in range(cfg.n_choices)]


def blank_reference_points_for(cfg, side=0):
    """Blank-paper point to the right of the last column of each row,
    at the same height as the bubbles."""
    _, n_side = side_question_range(cfg, side)
    cfg_side = dataclass_replace(cfg, n_questions=n_side)
    bubble_pts = bubble_centers_for(cfg, side=side)
    bd = bubble_d_native(cfg_side)
    last_bubble_x = row_x_shift(cfg_side) + bubble_x(cfg.n_choices - 1, cfg_side)
    # halfway between the last bubble and the sheet's right edge
    x_blank = last_bubble_x + (NATIVE_W - 6 - last_bubble_x) / 2 + bd / 2
    return {q: (x_blank, pts[0][1]) for q, pts in bubble_pts.items()}


def id_bit_cell_centers_for(cfg):
    """8 positions (x_mm, y_mm), in native mm (NOT in reportlab points,
    unlike bit_grid_geometry(), which is used for drawing)."""
    mark_x0 = NATIVE_W - MARK_MARGIN - MARK_SIZE
    mark_y0 = NATIVE_H - MARK_MARGIN - MARK_SIZE
    border_mm = ID_BORDER
    gap_mm = ID_GAP
    inner_mm = MARK_SIZE - 2 * (border_mm + gap_mm)
    cell_w = inner_mm / ID_COLS
    cell_h = inner_mm / ID_ROWS
    zone_x0 = border_mm + gap_mm
    zone_y0 = border_mm + gap_mm

    centers = []
    for k in range(N_ID_BITS):
        row = k // ID_COLS
        col = k % ID_COLS
        cx = mark_x0 + zone_x0 + (col + 0.5) * cell_w
        cy = mark_y0 + zone_y0 + (ID_ROWS - 1 - row + 0.5) * cell_h
        centers.append((cx, cy))
    return centers


def identity_zone_corners_for(cfg):
    x0, x1 = 3.0, NATIVE_W - 10.0
    y0, y1 = IDENTITY_Y - 5.0, IDENTITY_Y + 6.0
    return [(x0, y1), (x1, y1), (x1, y0), (x0, y0)]

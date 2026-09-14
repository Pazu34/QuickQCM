"""
Générateur modulaire de feuilles-réponses.

Reprend le design v2 (5 colonnes, ronds vides, en-têtes répétées, motif
numéroté) mais rend TOUT paramétrable :
- titre / sous-titre
- présence du champ "Classe :"
- nombre de questions (la mise en page se recalcule automatiquement)
- nombre de colonnes de réponses (2 à 5)
- échelle de la feuille (homothétie du design de base, natif = A6)
- nombre de feuilles par page imprimée (pavage rows x cols)
- format de la page de sortie (A4 par défaut)

Le design "natif" (échelle 1) reste exactement le format A6 déjà validé
(105 x 148.5 mm, bulles de 4.2mm). Toute autre échelle est une
homothétie pure de ce design : mêmes proportions, mêmes repères de
calage, juste plus grand ou plus petit.
"""
from dataclasses import dataclass, replace as dataclass_replace
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm as MM

# --- Dimensions natives (échelle 1), reprises du design A6 validé ---
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
ROW_H = 6.6                # hauteur de ligne native (12 questions, échelle 1)
BUBBLE_RATIO = BUBBLE_D / ROW_H   # rapport bulle/ligne, conservé à toute échelle

N_ID_BITS = 8
ID_BORDER = 1.0
ID_GAP = 0.5
ID_COLS, ID_ROWS = 4, 2

MIN_BUBBLE_MM = 3.15   # taille minimale imprimée d'une bulle pour une détection fiable
# (calé pour que 64 questions tiennent tout juste en recto-verso sur une A4 : 32+32
# donne des bulles à 3.186mm, un chouïa sous l'ancien seuil rond de 3.2mm)

CHOICES_MAX = ["A", "B", "C", "D", "E", "F"]  # jusqu'à 6 colonnes de réponses

# --- Code-barres de configuration (bas de la feuille) ---
# Encode n_questions, n_choices, l'échelle et show_classe directement sur
# la feuille : le script de lecture n'a plus besoin qu'on lui précise la
# configuration à la main, il la lit toute seule.
BARCODE_N_BITS = 15   # 6 (n_questions total, 1-64) + 2 (n_choices) + 4 (échelle) + 1 (classe) + 1 (face) + 1 (parité)
BARCODE_BAR_W = 1.5
BARCODE_BAR_GAP = 0.5
BARCODE_BAR_H = 4.0
BARCODE_Y_CENTER = 10.0   # mm, dans la bande verticale des repères du bas

SQRT2 = 2 ** 0.5

MAX_QUESTIONS = 64  # au-delà, aucun intérêt pratique -> tient sur 6 bits (1-64)
MAX_SCALE = 2.0      # taille maximale de la feuille-réponse : A4 (= A6 x2)


def needs_recto_verso(cfg):
    """True si le nombre de questions ne tient pas sur une seule face à
    l'échelle demandée (dans la limite MAX_SCALE = A4)."""
    return cfg.n_questions > max_questions_for_scale(min(cfg.scale, MAX_SCALE))


def side_question_range(cfg, side):
    """(numéro de la 1re question, nombre de questions) pour la face
    'side' (0 = recto, 1 = verso) de cfg. Si le recto-verso n'est pas
    nécessaire, side=0 retourne tout, side=1 est vide (0 question)."""
    total = cfg.n_questions
    if not needs_recto_verso(cfg):
        return (1, total) if side == 0 else (total + 1, 0)
    n1 = (total + 1) // 2
    n2 = total - n1
    return (1, n1) if side == 0 else (n1 + 1, n2)


def encode_config(cfg, side=0):
    """cfg -> un entier de 15 bits.
    [n_questions_total-1 (6b)][n_choices-3 (2b)][scale_level (4b)]
    [show_classe (1b)][side: 0=recto/1=verso (1b)][parité (1b)]."""
    if not (1 <= cfg.n_questions <= MAX_QUESTIONS):
        raise ValueError(f"n_questions doit \u00eatre entre 1 et {MAX_QUESTIONS} pour le code-barres.")
    n_q_code = cfg.n_questions - 1              # 0-63 (6 bits)
    n_choices_code = cfg.n_choices - 3          # 0-3 (pour 3 à 6 réponses)
    scale_level = round(math_log2(cfg.scale) * 2) + 8  # pas de sqrt(2), offset 8
    scale_level = max(0, min(15, scale_level))
    show_classe_bit = 1 if cfg.show_classe else 0
    side_bit = 1 if side else 0

    body = (n_q_code << 8) | (n_choices_code << 6) | (scale_level << 2) | (show_classe_bit << 1) | side_bit
    parity_bit = bin(body).count("1") % 2
    return (body << 1) | parity_bit


def decode_config_value(value):
    """entier de 15 bits -> (dict de paramètres + 'side', parité_ok: bool).
    'n_questions' dans le dict est le TOTAL (les 2 faces additionnées si
    recto-verso) ; 'side' indique quelle face a été lue (0=recto, 1=verso)."""
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
    """14 positions (x_mm, y_mm) natives des barres du code-barres,
    indépendantes de cfg (position FIXE, lisible avant même de connaître
    la configuration de la feuille)."""
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
    n_choices: int = 5          # nombre de colonnes de réponses (3 à 6)
    show_classe: bool = True
    scale: float = 1.0          # homothétie : 1.0 = format A6 natif
    tiles_rows: int = 2
    tiles_cols: int = 2
    page_w: float = 210.0       # page de sortie (mm) ; A4 par défaut
    page_h: float = 297.0

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
        """Retourne (page_w, page_h) à utiliser réellement : tels quels
        si le pavage tient dans cette orientation, sinon la page tournée
        à 90° si ça tient dans l'autre sens. Lève une erreur si aucune
        des deux orientations ne convient."""
        fit_w = self.tiles_cols * self.sheet_w
        fit_h = self.tiles_rows * self.sheet_h
        if fit_w <= self.page_w + 0.5 and fit_h <= self.page_h + 0.5:
            return self.page_w, self.page_h
        if fit_w <= self.page_h + 0.5 and fit_h <= self.page_w + 0.5:
            return self.page_h, self.page_w  # page tournée à 90°
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

        # Taille de bulle : vérifiée par FACE (pas sur le total), puisque
        # le recto-verso répartit les questions sur 2 pages si besoin.
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
# Mise en page dynamique (dépend uniquement de n_questions ; le reste du
# "chrome" — titre, identité, exemple, en-têtes — a une hauteur fixe).
# ---------------------------------------------------------------------

TITLE_Y = NATIVE_H - 11.0
SUBTITLE_Y = NATIVE_H - 16.0
IDENTITY_Y = NATIVE_H - 24.0
EXAMPLE_Y = NATIVE_H - 32.0
EXAMPLE_DIVIDER_Y = NATIVE_H - 36.0
GRID_TOP = NATIVE_H - 45.5     # y de la question 1
BOTTOM_MARGIN = MARK_MARGIN + MARK_SIZE + 2.0   # marge de sécurité sous la grille

HEADER_TEXT_OFFSET = 2.6   # décalage baseline du texte d'en-tête sous son point de référence
HEADER_CLEARANCE = 1.5     # marge visuelle mini entre le texte d'en-tête et le haut du rond suivant
MID_GAP_AFTER = 2.5     # espace entre la dernière question du 1er bloc et le trait
SPLIT_THRESHOLD = 7     # au-delà de ce nombre de questions, on scinde en 2 blocs avec rappel


MIN_TEXT_SHRINK_RATIO = 0.65  # le texte ne rétrécit jamais en dessous de 65% de sa taille nominale


def text_shrink_ratio(cfg):
    """Facteur de réduction du texte (numéros, en-têtes) dû au
    resserrement des lignes quand il y a beaucoup de questions, PLAFONNÉ
    à un minimum lisible (MIN_TEXT_SHRINK_RATIO) plutôt que de suivre
    indéfiniment la taille des bulles."""
    ratio = row_h_native(cfg.n_questions) / ROW_H
    return max(MIN_TEXT_SHRINK_RATIO, min(1.0, ratio))


def header_gap(cfg):
    """Espace vertical (mm, natif) nécessaire entre la position de
    référence d'une ligne d'en-tête et la 1re ligne de bulles qui suit,
    pour que le texte ne touche jamais les ronds. Utilise le MÊME
    facteur de réduction que le texte lui-même (avec son plancher de
    lisibilité) plutôt que la seule taille des bulles, sinon le texte
    "plafonné" (donc parfois plus grand que la bulle) pourrait déborder."""
    effective_half_bubble = (BUBBLE_D / 2) * text_shrink_ratio(cfg)
    return max(bubble_d_native(cfg) / 2, effective_half_bubble) + HEADER_TEXT_OFFSET + HEADER_CLEARANCE


def available_grid_height():
    return GRID_TOP - BOTTOM_MARGIN


def split_layout(n_questions):
    """Retourne (n1, n2) : nombre de questions dans le 1er et 2e bloc.
    n2=0 si pas de scission (peu de questions, un seul bloc suffit)."""
    if n_questions <= SPLIT_THRESHOLD:
        return n_questions, 0
    n1 = (n_questions + 1) // 2
    return n1, n_questions - n1


def row_h_native(n_questions):
    """Hauteur de ligne native (mm, échelle 1) pour que n_questions
    tiennent dans l'espace disponible, avec ou sans scission en 2 blocs.
    Pour le cas scindé, l'espace pris par l'en-tête de rappel dépend de
    la taille des bulles, qui dépend elle-même de row_h -> résolu par
    quelques itérations à point fixe (converge très vite)."""
    n1, n2 = split_layout(n_questions)
    avail = available_grid_height()
    if n2 == 0:
        return avail / n1
    row_h = avail / n_questions  # estimation initiale (sans le surcoût de l'en-tête)
    for _ in range(5):
        bd = BUBBLE_RATIO * row_h
        overhead = MID_GAP_AFTER + (bd / 2 + HEADER_TEXT_OFFSET + HEADER_CLEARANCE)
        row_h = (avail - overhead) / n_questions
    return row_h


def max_questions_for_scale(scale, min_bubble_mm=MIN_BUBBLE_MM):
    """Nombre maximal de questions tenant à cette échelle tout en gardant
    des bulles imprimées d'au moins `min_bubble_mm`."""
    avail = available_grid_height()
    min_row_h_native = min_bubble_mm / (BUBBLE_RATIO * scale)
    n_no_split = int(avail / min_row_h_native)
    if n_no_split <= SPLIT_THRESHOLD:
        return max(1, n_no_split)
    overhead = MID_GAP_AFTER + (min_row_h_native * BUBBLE_RATIO / 2 + HEADER_TEXT_OFFSET + HEADER_CLEARANCE)
    n_split = int((avail - overhead) / min_row_h_native)
    return max(1, n_split)


# ---------------------------------------------------------------------
# Dessin (toutes les fonctions reçoivent `s` = échelle de la feuille ;
# une coordonnée mm "native" x devient x*s*MM au moment du dessin).
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
    """Dessine le code-barres de configuration : 15 barres verticales
    fines (jamais confondues avec un repère carré), positionnées entre
    les deux repères du bas de la feuille."""
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
        # Toujours dessiner un contour, plein seulement si bit=1 : ça
        # garde chaque position repérable même à 0, utile pour un futur
        # alignement fin si besoin.
        c.setLineWidth(max(0.3, 0.4 * s))
        c.setFillColorRGB(0, 0, 0) if bit else c.setFillColorRGB(1, 1, 1)
        c.rect(bar_x, bar_y, bar_w, bar_h, fill=1, stroke=1)
    c.setFillColorRGB(0, 0, 0)


def draw_corner_marks(c, ox, oy, s, sheet_number=None):
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
        label = f"n\u00b0 {sheet_number}"
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
    """Décalage horizontal (mm, natif) du bloc numéro+bulles, calculé
    pour que ce bloc soit centré sur la largeur de la feuille, quel que
    soit le nombre de colonnes de réponses (3 à 6)."""
    bd = bubble_d_native(cfg)
    content_left_local = GRID_LEFT_LOCAL - 2.0   # même repère que label_left dans draw_bubble_row
    content_right_local = GRID_LEFT_LOCAL + COL_Q_LABEL_W + (cfg.n_choices - 1) * BUBBLE_GAP + bd
    content_w = content_right_local - content_left_local
    margin = 6.0   # même marge que les traits/lignes du reste de la feuille
    usable_w = NATIVE_W - 2 * margin
    desired_left = margin + (usable_w - content_w) / 2
    return desired_left - content_left_local


def bubble_x(i, cfg):
    """Position x LOCALE (mm, avant application du décalage de centrage
    row_x_shift) de la colonne i."""
    return GRID_LEFT_LOCAL + COL_Q_LABEL_W + i * BUBBLE_GAP + bubble_d_native(cfg) / 2


def bubble_d_native(cfg):
    """Diamètre natif (mm, échelle 1) des bulles, dépendant du nombre de
    questions : la hauteur de ligne varie avec n_questions (cf.
    row_h_native), et le diamètre de bulle suit dans le même rapport —
    plus de questions -> lignes plus fines -> bulles plus petites (mais
    jamais sous MIN_BUBBLE_MM/scale, cf. validate())."""
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
    """Échelle effective pour le texte (numéros, en-têtes de colonnes) :
    l'échelle globale de la feuille, réduite si les lignes sont plus
    fines que la référence à 12 questions — mais jamais en dessous d'un
    plancher lisible (cf. text_shrink_ratio). Ne grossit jamais au-delà
    de l'échelle normale quand les lignes sont plus généreuses (peu de
    questions) : seul le resserrement est corrigé."""
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


def draw_identity_field(c, ox, y_id, s, x0_local, x1_local, text=None):
    """Dessine le champ d'identité (Nom ou Classe) entre les abscisses
    locales x0_local/x1_local (mm, natif) : soit une ligne à remplir à la
    main (comportement par défaut, `text=None`), soit `text` imprimé
    directement dessus (nom/classe déjà connu depuis un CSV) — dans ce
    cas la ligne est omise, le texte la remplace."""
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
    draw_corner_marks(c, ox, oy, s, sheet_number=sheet_number)
    draw_config_barcode(c, ox, oy, s, cfg, side=side)

    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 11 * s)
    c.drawCentredString(ox + (NATIVE_W * s / 2) * MM, oy + TITLE_Y * s * MM, cfg.title)
    c.setFont("Helvetica", 8 * s)
    c.drawCentredString(ox + (NATIVE_W * s / 2) * MM, oy + SUBTITLE_Y * s * MM, cfg.subtitle)

    # --- Identité --- (nom/classe imprimés directement si fournis, sinon
    # ligne à remplir à la main, cf. draw_identity_field)
    y_id = oy + IDENTITY_Y * s * MM
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica", 8 * s)
    c.drawString(ox + 6 * s * MM, y_id, "Nom :")
    if cfg.show_classe:
        draw_identity_field(c, ox, y_id, s, 16, 63, nom)
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 8 * s)
        c.drawString(ox + 66 * s * MM, y_id, "Classe :")
        draw_identity_field(c, ox, y_id, s, 79, 90, classe)
    else:
        draw_identity_field(c, ox, y_id, s, 16, 90, nom)
    c.setFillColorRGB(0, 0, 0)

    # --- Config ajustée à cette face (même moteur, n_questions = celui de cette face) ---
    start_q, n_side = side_question_range(cfg, side)
    cfg_side = dataclass_replace(cfg, n_questions=n_side)

    # --- Exemple --- (bulles à la même taille que les questions -> cfg_side)
    example_filled = [1, 3] if cfg.n_choices >= 4 else [cfg.n_choices - 1]
    ex_letters = [cfg.choices[i] for i in example_filled]
    right_edge = draw_bubble_row(c, ox, oy, s, EXAMPLE_Y, "Exemple :", cfg_side,
                                  filled_indices=example_filled,
                                  label_font_size=7, center_shift_mm=-3)
    caption_x = right_edge + 2 * s * MM
    max_w = ox + (NATIVE_W - 2) * s * MM - caption_x
    prefix = f"= r\u00e9ponse{'s' if len(ex_letters) > 1 else ''}"
    letters_part = ' et '.join(ex_letters)
    full_text = f"{prefix} {letters_part}"

    font_sz = 7 * text_scale(cfg_side)
    while c.stringWidth(full_text, "Helvetica-Oblique", font_sz) > max_w and font_sz > 5.5 * text_scale(cfg_side):
        font_sz -= 0.3
    c.setFont("Helvetica-Oblique", font_sz)
    if c.stringWidth(full_text, "Helvetica-Oblique", font_sz) > max_w:
        # Toujours pas assez de place sur une ligne : retour à la ligne
        # après "réponse(s)" plutôt que de rogner le texte ou la taille.
        # Les deux lignes sont centrées entre caption_x et le bord droit.
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

    # --- Grille de questions (nombre variable, scindée en 2 si besoin) ---
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
        side_label = "Recto (1/2)" if side == 0 else "Verso (2/2)"
        c.drawCentredString(ox + (NATIVE_W * s / 2) * MM, oy + 3 * s * MM, side_label)
        c.setFillColorRGB(0, 0, 0)


def build(path, cfg, numbers=None):
    """numbers : liste de numéros (1-255), un par tuile de la page
    (longueur tiles_rows*tiles_cols), ou None pour des repères normaux."""
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
    """Génère un PDF multi-pages, une tuile = une feuille numérotée,
    numéros consécutifs à partir de start_number. n_sheets par défaut
    = tiles_rows*tiles_cols (une seule page).

    identities : {numero (int): (nom, classe)} optionnel — si fourni pour
    une feuille, son nom et/ou sa classe (l'un des deux peut être None)
    sont imprimés directement sur la fiche au lieu de la ligne à remplir
    à la main (cf. draw_identity_field).

    Si le nombre de questions ne tient pas sur une face (recto-verso
    nécessaire), chaque page de tuiles est immédiatement suivie de sa
    page verso correspondante (mêmes numéros, questions 2e moitié) —
    fonctionne nativement avec l'impression recto-verso automatique
    tant qu'il y a 1 seule feuille par page (cfg.tiles_rows=tiles_cols=1,
    le cas normal pour une feuille déjà à la taille maximale A4)."""
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
# Coordonnées canoniques (mm, échelle 1) pour le script de LECTURE.
# Utilisent exactement les mêmes fonctions que le dessin (bubble_x,
# row_x_shift, row_h_native, header_gap...) : générateur et lecteur ne
# peuvent donc plus diverger, contrairement à avant où sheet_layout_v2.py
# maintenait ses propres constantes séparément.
# ---------------------------------------------------------------------

def corner_points_for(cfg):
    return {
        "TL": (MARK_MARGIN + MARK_SIZE / 2, NATIVE_H - MARK_MARGIN - MARK_SIZE / 2),
        "TR": (NATIVE_W - MARK_MARGIN - MARK_SIZE / 2, NATIVE_H - MARK_MARGIN - MARK_SIZE / 2),
        "BL": (MARK_MARGIN + MARK_SIZE / 2, MARK_MARGIN + MARK_SIZE / 2),
        "BR": (NATIVE_W - MARK_MARGIN - SMALL_MARK_SIZE / 2, MARK_MARGIN + SMALL_MARK_SIZE / 2),
    }


def bubble_centers_for(cfg, side=0):
    """{numero_question GLOBAL (selon side_question_range): [(x_mm, y_mm) par colonne]}."""
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
    """Point de papier vierge à droite de la dernière colonne de chaque
    ligne, à la même hauteur que les bulles."""
    _, n_side = side_question_range(cfg, side)
    cfg_side = dataclass_replace(cfg, n_questions=n_side)
    bubble_pts = bubble_centers_for(cfg, side=side)
    bd = bubble_d_native(cfg_side)
    last_bubble_x = row_x_shift(cfg_side) + bubble_x(cfg.n_choices - 1, cfg_side)
    # à mi-chemin entre la dernière bulle et le bord droit de la feuille
    x_blank = last_bubble_x + (NATIVE_W - 6 - last_bubble_x) / 2 + bd / 2
    return {q: (x_blank, pts[0][1]) for q, pts in bubble_pts.items()}


def id_bit_cell_centers_for(cfg):
    """8 positions (x_mm, y_mm), en mm natifs (PAS en points reportlab -
    contrairement à bit_grid_geometry(), qui sert au dessin)."""
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

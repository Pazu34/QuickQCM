"""
Canonical coordinates for the v2 grid (5 columns, empty circles,
separate headers, numbered pattern in the top-right corner), in mm,
origin at bottom-left.

Reuses exactly the geometry of generate_sheet_a6_v2.py -- any change to
that file must be mirrored here.
"""

A6_W = 105.0
A6_H = 148.5

MARK_SIZE = 7.0
MARK_MARGIN = 6.5
SMALL_MARK_SIZE = 3.5

N_QUESTIONS = 12
CHOICES = ["A", "B", "C", "D", "E"]
BUBBLE_D = 4.2
BUBBLE_GAP = 11.0
COL_Q_LABEL_W = 8.0
GRID_LEFT_LOCAL = 8.0
ROW_X_SHIFT = 10.0

GRID_TOP_LOCAL = A6_H - 45.5   # top of the grid, question 1's row
ROW_H = 6.6
MID_GAP_AFTER_Q6 = 2.5         # space between Q6 and the mid-recap divider line
MID_TO_Q7_GAP = 5.0            # space between the mid-recap and Q7

# Numbered pattern (top-right corner)
N_ID_BITS = 8
ID_BORDER = 1.0
ID_GAP = 0.5
ID_COLS, ID_ROWS = 4, 2

# Blank-paper sampling point (to the right of column E)
BLANK_REF_X = 85.0


def corner_points():
    """4 alignment points, in mm. Keys: 'TL', 'TR' (numbered pattern),
    'BL' (QR-like patterns) and 'BR' (small solid square)."""
    return {
        "TL": (MARK_MARGIN + MARK_SIZE / 2, A6_H - MARK_MARGIN - MARK_SIZE / 2),
        "TR": (A6_W - MARK_MARGIN - MARK_SIZE / 2, A6_H - MARK_MARGIN - MARK_SIZE / 2),
        "BL": (MARK_MARGIN + MARK_SIZE / 2, MARK_MARGIN + MARK_SIZE / 2),
        "BR": (A6_W - MARK_MARGIN - SMALL_MARK_SIZE / 2, MARK_MARGIN + SMALL_MARK_SIZE / 2),
    }


def _bubble_x(i):
    return ROW_X_SHIFT + GRID_LEFT_LOCAL + COL_Q_LABEL_W + i * BUBBLE_GAP + BUBBLE_D / 2


def _row_y(q):
    """y (mm) of question q's row (1..12), handles the split/recap after
    question 6."""
    if q <= 6:
        return GRID_TOP_LOCAL - (q - 1) * ROW_H
    mid_y = GRID_TOP_LOCAL - 6 * ROW_H - MID_GAP_AFTER_Q6
    grid_top_local2 = mid_y - MID_TO_Q7_GAP
    return grid_top_local2 - (q - 7) * ROW_H


def bubble_centers():
    """{question_number (1..12): [(x_mm, y_mm) for A,B,C,D,E]}."""
    result = {}
    for q in range(1, N_QUESTIONS + 1):
        y = _row_y(q)
        result[q] = [(_bubble_x(i), y) for i in range(len(CHOICES))]
    return result


def example_bubble_centers():
    """'Example:' row, same geometry as a normal question."""
    y = A6_H - 32.0
    return [(_bubble_x(i), y) for i in range(len(CHOICES))]


def blank_reference_points():
    """{question_number (1..12): (x_mm, y_mm)} of a blank-paper point at
    the height of each answer row."""
    return {q: (BLANK_REF_X, _row_y(q)) for q in range(1, N_QUESTIONS + 1)}


def id_bit_cell_centers():
    """8 positions (x_mm, y_mm) of the numbered pattern's cells
    (top-right corner), in order from bit 7 (MSB) to bit 0."""
    mark_x0 = A6_W - MARK_MARGIN - MARK_SIZE  # pattern's bottom-left
    mark_y0 = A6_H - MARK_MARGIN - MARK_SIZE
    inner = MARK_SIZE - 2 * (ID_BORDER + ID_GAP)
    cell_w = inner / ID_COLS
    cell_h = inner / ID_ROWS
    zone_x0 = ID_BORDER + ID_GAP
    zone_y0 = ID_BORDER + ID_GAP

    centers = []
    for k in range(N_ID_BITS):
        row = k // ID_COLS
        col = k % ID_COLS
        cx = mark_x0 + zone_x0 + (col + 0.5) * cell_w
        cy = mark_y0 + zone_y0 + (ID_ROWS - 1 - row + 0.5) * cell_h
        centers.append((cx, cy))
    return centers


# "Nom : ____ Classe : ____" ("Name:"/"Class:") area (bottom-left corner,
# top-right corner of the rectangle to extract), in mm, generous margin
# around the printed text and the lines to fill in.
IDENTITY_ZONE = {
    "x0": 3.0, "y0": A6_H - 24.0 - 5.0,
    "x1": 95.0, "y1": A6_H - 24.0 + 6.0,
}


def identity_zone_corners():
    """4 corners (mm) of the Name/Class area, in order top-left,
    top-right, bottom-right, bottom-left."""
    z = IDENTITY_ZONE
    return [
        (z["x0"], z["y1"]), (z["x1"], z["y1"]),
        (z["x1"], z["y0"]), (z["x0"], z["y0"]),
    ]

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
INPUT_DIR = ROOT_DIR / "data"
OUTPUT_DIR = ROOT_DIR / "output"
DATASETS_DIR = OUTPUT_DIR / "datasets"
UPLOAD_FORM_PATH = ROOT_DIR / "upload_form.html"
SCHOOL_FILES_GLOB = "*_school_updated_call.csv"

META_COLUMNS = ["call", "grade", "class", "student_type"]
N_META_COLUMNS = len(META_COLUMNS)

SUBJECT_NAME_ROW, UNIT_ROW, LO_LIMIT_ROW, HI_LIMIT_ROW = 0, 1, 2, 3
STUDENT_DATA_START_ROW = 6

COLS_PER_ROW = 5
CELL_ASPECT_W, CELL_ASPECT_H = 16, 11

LINE_COLOR = "royalblue"
LIMIT_COLOR = "red"
MARKER_SIZE = 5
LIMIT_LINE_WIDTH = 1
X_RANGE_PADDING_RATIO = 0.15
TITLE_FONT_SIZE = 11

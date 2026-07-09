"""Output file naming helpers for generated Honey reports."""
import datetime
import re
from pathlib import Path

_TS_RE = re.compile(r"_\d{6}_\d{4}$")
_MAX_NAME_LEN = 20


def _timestamp():
    """Current timestamp for filenames: 260601_0949 (YYMMDD_HHMM)."""
    return datetime.datetime.now().strftime("%y%m%d_%H%M")


def suggest_base_name(csv_paths, group=None):
    """Build a default report base name: name (<=20 chars) + timestamp.

    The name portion is taken from the first input/source name and capped at
    20 characters so the shown Save Name stays short; the date/time suffix is
    always appended (and satisfies _TS_RE so build_output_path won't re-add it).
    """
    if group is not None and group.names():
        base = group.names()[0]
    elif csv_paths:
        base = Path(csv_paths[0]).stem
    else:
        base = "report"
    base = base.strip(" _-")[:_MAX_NAME_LEN].strip(" _-") or "report"
    return f"{base}_{_timestamp()}"


def build_output_path(out_dir, base):
    """Return final xlsx path for a user-provided base name."""
    base = base.strip()
    if base.lower().endswith(".xlsx"):
        base = base[:-5]
    base = base.strip(" _-") or "report"
    if not _TS_RE.search(base):
        base = f"{base}_{_timestamp()}"
    return str(Path(out_dir) / f"{base}.xlsx")

"""Output file naming helpers for generated Honey reports."""
import datetime
import re
from pathlib import Path

_TS_RE = re.compile(r"_\d{6}_\d{4}$")


def _timestamp():
    """Current timestamp for filenames: 260601_0949 (YYMMDD_HHMM)."""
    return datetime.datetime.now().strftime("%y%m%d_%H%M")


def suggest_base_name(csv_paths, group=None):
    """Build a default report base name from the first input/source name."""
    if group is not None and group.names():
        base = group.names()[0]
    elif csv_paths:
        base = Path(csv_paths[0]).stem
    else:
        base = "report"
    base = base.strip(" _-") or "report"
    return f"{base}_report_{_timestamp()}"


def build_output_path(out_dir, base):
    """Return final xlsx path for a user-provided base name."""
    base = base.strip()
    if base.lower().endswith(".xlsx"):
        base = base[:-5]
    base = base.strip(" _-") or "report"
    if not _TS_RE.search(base):
        base = f"{base}_{_timestamp()}"
    return str(Path(out_dir) / f"{base}.xlsx")

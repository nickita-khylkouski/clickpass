"""
Display helpers: banners, histograms, tables, and data quality reports.

Uses Rich for enhanced output when available; falls back to plain text.
Extracted and improved from hackathon-elo/pipeline_v2/shared.py.
"""

from __future__ import annotations

import sys
from typing import Any, Sequence

# Rich is optional -- listed under [project.optional-dependencies] "full".
try:
    from rich.console import Console
    from rich.table import Table

    HAS_RICH = True
except ImportError:
    HAS_RICH = False


# Shared console instance (only created when Rich is available).
_console: Any = Console(stderr=True) if HAS_RICH else None


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def print_banner(text: str) -> None:
    """Print a boxed section header.

    Uses a Rich panel when available, otherwise plain ASCII box.
    """
    if HAS_RICH and _console is not None:
        from rich.panel import Panel

        _console.print(Panel(text, expand=True, style="bold cyan"))
    else:
        width = 70
        print(f"\n{'=' * width}", file=sys.stderr)
        print(f"  {text}", file=sys.stderr)
        print(f"{'=' * width}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Histogram
# ---------------------------------------------------------------------------

def print_histogram(
    values: Sequence[float | int],
    bins: int = 20,
    label: str = "Score",
) -> None:
    """Print an ASCII histogram of *values* to stderr.

    Parameters
    ----------
    values:
        Numeric values to bin.
    bins:
        Number of bins (default 20).
    label:
        Label shown in the header line.
    """
    if not values:
        print("  (no values)", file=sys.stderr)
        return

    mn = min(values)
    mx = max(values)
    if mn == mx:
        print(f"  All values = {mn}", file=sys.stderr)
        return

    bin_width = (mx - mn) / bins
    counts = [0] * bins
    for v in values:
        idx = min(int((v - mn) / bin_width), bins - 1)
        counts[idx] += 1

    max_count = max(counts)
    bar_chars = 40

    print(f"\n  {label} Distribution ({len(values)} values, range {mn:.1f}-{mx:.1f}):", file=sys.stderr)
    for i, count in enumerate(counts):
        lo = mn + i * bin_width
        hi = lo + bin_width
        bar = "\u2588" * int(count / max_count * bar_chars) if max_count > 0 else ""
        print(f"  {lo:6.1f}-{hi:6.1f} | {bar} {count}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def print_summary_table(
    rows: Sequence[Sequence[Any]],
    columns: Sequence[str],
) -> None:
    """Print a formatted table to stderr.

    Uses a Rich table when available, otherwise formats columns with fixed
    width based on content.

    Parameters
    ----------
    rows:
        List of row tuples / lists.  Each row must have the same number of
        elements as *columns*.
    columns:
        Column header names.
    """
    if not rows:
        print("  (no data)", file=sys.stderr)
        return

    if HAS_RICH and _console is not None:
        table = Table(show_header=True, header_style="bold")
        for col in columns:
            table.add_column(str(col))
        for row in rows:
            table.add_row(*(str(cell) for cell in row))
        _console.print(table)
    else:
        _print_plain_table(rows, columns)


def _print_plain_table(
    rows: Sequence[Sequence[Any]],
    columns: Sequence[str],
) -> None:
    """Render a table using plain text alignment."""
    # Determine column widths.
    str_rows = [[str(cell) for cell in row] for row in rows]
    widths = [len(col) for col in columns]
    for row in str_rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    def _fmt_row(cells: Sequence[str]) -> str:
        parts = []
        for i, cell in enumerate(cells):
            w = widths[i] if i < len(widths) else len(cell)
            parts.append(str(cell).ljust(w))
        return "  " + "  ".join(parts)

    header = _fmt_row(columns)
    sep = "  " + "  ".join("-" * w for w in widths)

    print(header, file=sys.stderr)
    print(sep, file=sys.stderr)
    for row in str_rows:
        print(_fmt_row(row), file=sys.stderr)


# ---------------------------------------------------------------------------
# Data quality report
# ---------------------------------------------------------------------------

def print_data_quality_report(people: list[dict]) -> None:
    """Print a detailed data quality summary to stderr.

    Shows coverage statistics for key fields (email, linkedin_url,
    github_url, company, role, location, x_handle) and flags people
    who have neither LinkedIn nor GitHub as "unverifiable".

    Parameters
    ----------
    people:
        List of person dicts as produced by the pipeline.
    """
    total = len(people)

    if total == 0:
        print("\n  Data Quality Report: 0 people (empty dataset)", file=sys.stderr)
        return

    # Define fields to check
    fields = [
        ("email", "email"),
        ("linkedin_url", "linkedin_url"),
        ("github_url", "github_url"),
        ("company", "company"),
        ("role", "role"),
        ("location", "location"),
        ("x_handle", "x_handle"),
    ]

    # Count coverage for each field
    counts: dict[str, int] = {}
    for label, key in fields:
        counts[label] = sum(1 for p in people if p.get(key))

    # Count unverifiable: no linkedin AND no github
    unverifiable = sum(
        1 for p in people
        if not p.get("linkedin_url") and not p.get("github_url")
    )

    # Build report rows: (Field, Count, Percentage)
    rows = []
    for label, _ in fields:
        count = counts[label]
        pct = count * 100 / total
        rows.append((label, str(count), f"{pct:.0f}%"))

    # Add unverifiable row
    unverifiable_pct = unverifiable * 100 / total
    rows.append(("unverifiable (no linkedin + no github)", str(unverifiable), f"{unverifiable_pct:.0f}%"))

    # Print using Rich table if available, otherwise plain text
    header_text = f"Data Quality Report: {total} people"

    if HAS_RICH and _console is not None:

        table = Table(
            title=header_text,
            show_header=True,
            header_style="bold",
        )
        table.add_column("Field", style="cyan")
        table.add_column("Count", justify="right")
        table.add_column("Pct", justify="right")

        for label, count_str, pct_str in rows:
            style = "red" if label.startswith("unverifiable") else None
            table.add_row(label, count_str, pct_str, style=style)

        _console.print(table)
    else:
        print(f"\n  {header_text}", file=sys.stderr)
        print(f"  {'=' * 55}", file=sys.stderr)
        for label, count_str, pct_str in rows:
            flag = " [!]" if label.startswith("unverifiable") else ""
            print(f"  {label:<40s} {count_str:>4s}  {pct_str:>5s}{flag}", file=sys.stderr)
        print(f"  {'=' * 55}", file=sys.stderr)

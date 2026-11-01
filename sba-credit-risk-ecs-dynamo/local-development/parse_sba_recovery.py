"""Turn SBA's published recovery tables into one tidy CSV the pipeline can read.

    data/raw/sba_recovery/WDS_PostChargeOffRecoveryRates_Report_*.pdf   Table 11, by charge-off year
    data/raw/sba_recovery/WDS_RecoveryRates_Report_*.pdf                Table 10, by purchase year
      ->  data/sba_recovery_rates.csv

Table 11 is the one that nets down the loan book: recoveries after charge-off as a percent of the
amount charged off. Table 10 is a different ratio, total recoveries as a percent of the amount SBA
purchased on its guarantee. They have different denominators and must never be added together.

The CSV is committed, so neither ``pypdf`` nor the PDFs are needed after clone. Every row of both
tables prints its own Total, which makes the parse self-checking: the yearly rates of each row
must sum to that Total to within rounding, or the script stops.
"""

import csv
import re
from pathlib import Path

import pypdf

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / "data" / "raw" / "sba_recovery"
OUTPUT = PROJECT / "data" / "sba_recovery_rates.csv"

TABLES = {
    "post_charge_off": "WDS_PostChargeOffRecoveryRates_Report_*.pdf",
    "total_recovery": "WDS_RecoveryRates_Report_*.pdf",
}
FISCAL_YEARS = list(range(2016, 2026))  # the report's ten columns; FY2025 is as of 2025-06-30
ROUNDING_TOLERANCE = 0.06  # percentage points; ten values each rounded to 0.01

ROW = re.compile(r"^(20\d\d)\s+(.+)$")
VALUE = re.compile(r"\(\s*[\d.,]+%\)|[\d.,]+%|N/A")
NOT_A_PROGRAM = ("Fiscal Year", "Charge off Year", "Purchase Year", "Table ", "This table")


def to_percent(token):
    if token == "N/A":
        return None
    number = float(token.strip("()% ").replace(",", ""))
    return -number if token.startswith("(") else number


def text_lines(path):
    lines = []
    for page in pypdf.PdfReader(str(path)).pages:
        for line in (page.extract_text() or "").splitlines():
            line = re.sub(r"^\s*\d*\s*(?=Table )", "", line.strip())
            if line and lines and not VALUE.sub("", line).strip():
                lines[-1] += " " + line  # the PDF wrapped a long row; the Total check proves the join
            else:
                lines.append(line)
    return lines


def parse_rows(path):
    program = None
    pending = None
    for line in text_lines(path):
        match = ROW.match(line)
        if not match:
            pending = None if line.startswith(NOT_A_PROGRAM) or not line else line
            continue
        program, pending = pending or program, None
        values = [to_percent(token) for token in VALUE.findall(match[2])]
        if len(values) != len(FISCAL_YEARS) + 1:
            raise ValueError(f"{path.name}: {program} {match[1]}: {len(values)} cells in {line!r}")
        yield program, int(match[1]), values[:-1], values[-1]


def main():
    rows = []
    for table, pattern in TABLES.items():
        path = next(SOURCE.glob(pattern))
        for program, cohort, yearly, total in parse_rows(path):
            reported = [value for value in yearly if value is not None]
            if total is not None and abs(sum(reported) - total) > ROUNDING_TOLERANCE:
                raise ValueError(f"{table} {program} {cohort}: {sum(reported):.2f} != {total}")
            rows += [
                {
                    "table": table,
                    "program": program,
                    "cohort_year": cohort,
                    "fiscal_year": year,
                    "rate_pct": value,
                    "cohort_total_pct": total,
                }
                for year, value in zip(FISCAL_YEARS, yearly)
                if value is not None
            ]
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"{OUTPUT.name}: {len(rows):,} rows, {len({row['program'] for row in rows})} programs")


if __name__ == "__main__":
    main()

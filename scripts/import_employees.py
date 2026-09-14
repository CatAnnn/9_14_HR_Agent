from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.schemas.employee_database import EmployeeDatabaseUpsertRequest  # noqa: E402
from backend.schemas.profile import EmployeeProfile  # noqa: E402
from backend.services.employee_database_service import EmployeeDatabaseService  # noqa: E402

NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
NS_PACKAGE_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
EMPLOYEE_IMPORT_MAPPING_VERSION = "v5-deterministic-goal-format"

DATE_HEADERS = {
    "Labor Contract Termination Date",
    "Start Date in Bosch",
    "Position Since",
}

EMPLOYEE_HEADERS = [
    "Personal No.",
    "Name",
    "Legal Entity",
    "GB",
    "Department",
    "Job Grade",
    "Position",
    "HRBP",
    "Leadership Flag",
    "Target Manager",
    "Diciplinary Manager",
    "Labor Contract Termination Date",
    "Start Date in Bosch",
    "Gender",
    "Age",
    "Position Since",
    "Function by Person",
    "历史博世工作经历",
    "Goal",
    "Current Career Elements",
    "Talent Pool",
    "TCL (SLx)",
    "Performance Rating (A Group)",
    "History ASR Rating",
]

OPTIONAL_EMPLOYEE_HEADERS = {"Current Career Elements"}

EMPLOYEE_HEADER_ALIASES = {
    "Career Element": "Current Career Elements",
    "Career Elements": "Current Career Elements",
}

GOAL_TABLE_HEADERS = (
    "goal/目标",
    "actionplan/目标行动计划",
    "bottom/保底值",
    "meetexpectation/达标值",
    "challenging/挑战值",
)
GOAL_TABLE_GROUPS = (
    ("action_plan", "Action Plan/目标行动计划"),
    ("bottom", "Bottom/保底值"),
    ("meet_expectation", "Meet Expectation/达标值"),
    ("challenging", "Challenging/挑战值"),
)
GOAL_POINT_PREFIX = re.compile(
    r"^(?:[-*•·▪◦‣●○■□◆◇✓✔]\s*|(?:\d{1,3}[a-z]?|[a-z])[.)、），,]\s*)",
    re.IGNORECASE,
)


def clean_cell(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def _normalize_goal_newlines(value: str) -> str:
    return (
        value.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


def _goal_cell_lines(value: str) -> list[str]:
    return [line.strip() for line in _normalize_goal_newlines(value).split("\n") if line.strip()]


def _strip_spreadsheet_text_envelope(value: str) -> str:
    text = value.strip()
    for opening, closing in (("\"", "\""), ("“", "”"), ("'", "'"), ("‘", "’")):
        if text.startswith(opening) and text.endswith(closing):
            return text[len(opening):-len(closing)].strip()
    return text


def _goal_table_header_key(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _parse_flattened_goal_table(value: str) -> list[dict[str, str]] | None:
    text = _strip_spreadsheet_text_envelope(_normalize_goal_newlines(value))
    header, separator, body = text.partition("\n")
    if not separator:
        return None
    headers = tuple(_goal_table_header_key(cell) for cell in header.split("\t"))
    if headers != GOAL_TABLE_HEADERS:
        return None

    cells = body.strip().split("\t")
    if len(cells) < len(GOAL_TABLE_HEADERS):
        return None

    rows: list[dict[str, str]] = []
    current_goal = cells[0]
    cursor = 1
    while cursor + 3 < len(cells):
        action_plan = cells[cursor]
        bottom = cells[cursor + 1]
        meet_expectation = cells[cursor + 2]
        challenge_and_next_goal = cells[cursor + 3]
        has_next_row = cursor + 4 < len(cells)
        challenging = challenge_and_next_goal
        next_goal = ""

        if has_next_row:
            combined_lines = _goal_cell_lines(challenge_and_next_goal)
            threshold_line_count = max(
                1,
                len(_goal_cell_lines(bottom)),
                len(_goal_cell_lines(meet_expectation)),
            )
            if len(combined_lines) <= threshold_line_count:
                return None
            challenging = "\n".join(combined_lines[:threshold_line_count])
            next_goal = "\n".join(combined_lines[threshold_line_count:])

        if not _goal_cell_lines(current_goal):
            return None
        rows.append(
            {
                "goal": current_goal,
                "action_plan": action_plan,
                "bottom": bottom,
                "meet_expectation": meet_expectation,
                "challenging": challenging,
            }
        )
        current_goal = next_goal
        cursor += 4

    if cursor != len(cells) or not rows:
        return None
    return rows


def _format_goal_points(value: str) -> list[str]:
    return [line if GOAL_POINT_PREFIX.match(line) else f"- {line}" for line in _goal_cell_lines(value)]


def _format_flattened_goal_table(rows: list[dict[str, str]]) -> str:
    blocks: list[str] = []
    for row in rows:
        goal_lines = _goal_cell_lines(row["goal"])
        if not goal_lines:
            continue
        block = [f"{goal_lines[0]}\t", *_format_goal_points("\n".join(goal_lines[1:]))]
        for key, label in GOAL_TABLE_GROUPS:
            points = _format_goal_points(row[key])
            if points:
                block.extend((label, *points))
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def normalize_goal_format(value: str | None) -> str:
    """Normalize goal layout deterministically without rewriting goal content."""
    text = _normalize_goal_newlines(clean_cell(value))
    if not text:
        return ""

    table_rows = _parse_flattened_goal_table(text)
    if table_rows:
        return _format_flattened_goal_table(table_rows)

    normalized_lines: list[str] = []
    previous_blank = False
    for raw_line in text.split("\n"):
        line = raw_line.rstrip(" ")
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        normalized_lines.append("" if is_blank else line)
        previous_blank = is_blank
    return "\n".join(normalized_lines).strip("\n")


def split_list(value: str | None) -> list[str]:
    text = clean_cell(value)
    if not text:
        return []
    return [item.strip() for item in re.split(r"[\n;；]+", text) if item.strip()]


def normalize_date_cell(header: str, value: str) -> str:
    if header not in DATE_HEADERS or not value:
        return value
    if re.fullmatch(r"\d+(?:\.0)?", value):
        days = int(float(value))
        try:
            return (datetime(1899, 12, 30) + timedelta(days=days)).date().isoformat()
        except OverflowError:
            return value
    return value


def normalize_row(row: dict[str, str]) -> dict[str, str]:
    return {header: normalize_date_cell(header, clean_cell(row.get(header))) for header in EMPLOYEE_HEADERS}


def validate_headers(headers: list[str]) -> list[str]:
    normalized_headers = [EMPLOYEE_HEADER_ALIASES.get(header, header) for header in headers]
    missing = [
        header
        for header in EMPLOYEE_HEADERS
        if header not in normalized_headers and header not in OPTIONAL_EMPLOYEE_HEADERS
    ]
    unexpected = [
        header
        for header, normalized_header in zip(headers, normalized_headers)
        if header and normalized_header not in EMPLOYEE_HEADERS
    ]
    seen: set[str] = set()
    duplicate_headers: list[str] = []
    for header in normalized_headers:
        if not header:
            continue
        if header in seen and header not in duplicate_headers:
            duplicate_headers.append(header)
        seen.add(header)
    if missing or unexpected or duplicate_headers:
        message_parts = []
        if missing:
            message_parts.append("missing headers: " + ", ".join(missing))
        if unexpected:
            message_parts.append("unexpected headers: " + ", ".join(unexpected))
        if duplicate_headers:
            message_parts.append(
                "duplicate headers after normalization: " + ", ".join(duplicate_headers)
            )
        raise SystemExit(
            "Employee file headers must match the configured header list exactly; "
            + "; ".join(message_parts)
        )
    return normalized_headers


def load_rows(path: Path) -> list[dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return load_csv(path)
    if suffix == ".xlsx":
        return load_xlsx(path)
    raise SystemExit(f"Unsupported employee file type: {suffix}. Use .xlsx or .csv")


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        raw_headers = reader.fieldnames or []
        headers = validate_headers([clean_cell(header) for header in raw_headers])
        rows: list[dict[str, str]] = []
        for raw in reader:
            row = normalize_row(
                {
                    header: clean_cell(raw.get(raw_header))
                    for raw_header, header in zip(raw_headers, headers)
                    if header
                }
            )
            if any(row.values()):
                rows.append(row)
        return rows


def load_xlsx(path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as zf:
        shared = read_shared_strings(zf)
        sheet_path = first_sheet_path(zf)
        sheet = ET.fromstring(zf.read(sheet_path))
        raw_rows: list[dict[int, str]] = []
        for row_node in sheet.findall(f".//{NS_MAIN}sheetData/{NS_MAIN}row"):
            values: dict[int, str] = {}
            for cell in row_node.findall(f"{NS_MAIN}c"):
                ref = cell.attrib.get("r", "")
                col_index = column_index(ref)
                if col_index is None:
                    continue
                value = read_cell(cell, shared)
                values[col_index] = clean_cell(value)
            if any(values.values()):
                raw_rows.append(values)
        if not raw_rows:
            return []
        max_col = max(max(row.keys(), default=0) for row in raw_rows)
        headers = validate_headers(
            [clean_cell(raw_rows[0].get(idx, "")) for idx in range(max_col + 1)]
        )
        rows: list[dict[str, str]] = []
        for raw in raw_rows[1:]:
            row = normalize_row({headers[idx]: clean_cell(raw.get(idx, "")) for idx in range(max_col + 1) if headers[idx]})
            if any(row.values()):
                rows.append(row)
        return rows


def read_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for si in root.findall(f"{NS_MAIN}si"):
        parts = [node.text or "" for node in si.findall(f".//{NS_MAIN}t")]
        strings.append("".join(parts))
    return strings


def first_sheet_path(zf: zipfile.ZipFile) -> str:
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    first_sheet = workbook.find(f"{NS_MAIN}sheets/{NS_MAIN}sheet")
    if first_sheet is None:
        raise SystemExit("Workbook has no sheets")
    rid = first_sheet.attrib.get(f"{NS_REL}id")
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.findall(f"{NS_PACKAGE_REL}Relationship"):
        if rel.attrib.get("Id") == rid:
            target = rel.attrib.get("Target", "")
            target = target.lstrip("/")
            return target if target.startswith("xl/") else f"xl/{target}"
    return "xl/worksheets/sheet1.xml"


def read_cell(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(f".//{NS_MAIN}t"))
    value_node = cell.find(f"{NS_MAIN}v")
    if value_node is None or value_node.text is None:
        return ""
    raw = value_node.text
    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return raw
    return raw


def column_index(ref: str) -> int | None:
    match = re.match(r"([A-Z]+)", ref.upper())
    if not match:
        return None
    total = 0
    for char in match.group(1):
        total = total * 26 + (ord(char) - ord("A") + 1)
    return total - 1


def build_profile(row: dict[str, str]) -> EmployeeProfile:
    return EmployeeProfile(
        employee_alias=row.get("Name") or None,
        role=row.get("Position") or None,
        department=row.get("Department") or None,
        level=row.get("Job Grade") or None,
        reporting_line=row.get("Diciplinary Manager") or row.get("Target Manager") or None,
        performance_rating=row.get("Performance Rating (A Group)") or None,
        tcl=row.get("TCL (SLx)") or None,
        key_goals=split_list(normalize_goal_format(row.get("Goal"))),
        past_ratings=split_list(row.get("History ASR Rating")),
        historical_feedback=split_list(row.get("历史博世工作经历")),
        current_career_elements=split_list(row.get("Current Career Elements")),
    )


def build_profile_text(row: dict[str, str]) -> str:
    values = {
        header: normalize_goal_format(row.get(header)) if header == "Goal" else row.get(header, "")
        for header in EMPLOYEE_HEADERS
    }
    return "\n".join(f"{header}：{values[header]}" for header in EMPLOYEE_HEADERS if values[header])


def to_payload(row: dict[str, str]) -> EmployeeDatabaseUpsertRequest:
    employee_id = clean_cell(row.get("Personal No."))
    if not employee_id:
        raise ValueError("missing Personal No.")
    profile_text = build_profile_text(row)
    profile = build_profile(row)
    profile.source_profile_text = profile_text
    return EmployeeDatabaseUpsertRequest(
        employee_id=employee_id,
        employee_alias=row.get("Name") or None,
        name=row.get("Name") or None,
        department=row.get("Department") or None,
        role=row.get("Position") or None,
        manager=row.get("Diciplinary Manager") or row.get("Target Manager") or None,
        profile_text=profile_text,
        profile=profile,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Import employee master data from the configured Bosch Excel/CSV headers.")
    parser.add_argument("file", type=Path, help="Path to .xlsx or .csv file")
    parser.add_argument("--dry-run", action="store_true", help="Parse and validate only; do not write database")
    parser.add_argument(
        "--exact",
        action="store_true",
        help="Make PostgreSQL exactly match the employee IDs in this file.",
    )
    parser.add_argument(
        "--skip-unchanged",
        action="store_true",
        help="Deprecated compatibility flag; --exact is always SHA-256 guarded.",
    )
    parser.add_argument("--quiet", action="store_true", help="Print compact summary without sample employee records")
    args = parser.parse_args()

    path = args.file
    if not path.exists():
        raise SystemExit(f"File not found: {path}")

    content_sha256 = hashlib.sha256(
        EMPLOYEE_IMPORT_MAPPING_VERSION.encode("utf-8")
        + b"\0"
        + path.read_bytes()
    ).hexdigest()
    rows = load_rows(path)
    parsed: list[tuple[int, EmployeeDatabaseUpsertRequest]] = []
    errors: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=2):
        try:
            parsed.append((index, to_payload(row)))
        except Exception as exc:  # noqa: BLE001
            errors.append({"row": str(index), "error": str(exc)})

    if args.exact and not parsed and not errors:
        errors.append(
            {
                "row": "file",
                "error": "Exact employee sync requires at least one employee.",
            }
        )

    service = None if args.dry_run else EmployeeDatabaseService()
    imported = 0
    pruned = 0
    changed = True
    samples: list[dict[str, object]] = []

    if args.exact:
        if not errors:
            payloads = [payload for _, payload in parsed]
            if service is None:
                imported = len(payloads)
                samples = [
                    payload.model_dump(mode="json")
                    for payload in payloads[:3]
                ]
            else:
                try:
                    records, pruned, changed = service.sync_exact_if_changed(
                        payloads,
                        source_path=str(path.resolve()),
                        content_sha256=content_sha256,
                    )
                    imported = len(records)
                    samples = [
                        record.model_dump(mode="json")
                        for record in records[:3]
                    ]
                except Exception as exc:  # noqa: BLE001
                    errors.append({"row": "sync", "error": str(exc)})
    else:
        for index, payload in parsed:
            try:
                if service is not None:
                    record = service.upsert(payload)
                    if len(samples) < 3:
                        samples.append(record.model_dump(mode="json"))
                elif len(samples) < 3:
                    samples.append(payload.model_dump(mode="json"))
                imported += 1
            except Exception as exc:  # noqa: BLE001
                errors.append({"row": str(index), "error": str(exc)})

    summary = {
        "file": str(path),
        "dry_run": args.dry_run,
        "exact": args.exact,
        "rows": len(rows),
        "imported": imported,
        "pruned": pruned,
        "changed": changed,
        "content_sha256": content_sha256,
        "errors": errors,
    }
    if not args.quiet:
        summary["samples"] = samples
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

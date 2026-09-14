from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.user_content_export_service import ExportAuditContext  # noqa: E402
from backend.services.user_record_export_service import (  # noqa: E402
    ExportSelection,
    UserRecordExportService,
)


def _calendar_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "日期必须使用 YYYY-MM-DD 格式"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export readable HR Agent user records by user and date."
    )
    parser.add_argument(
        "--requested-by",
        required=True,
        help="Email address of the active administrator requesting the export.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Destination ZIP path. Existing files are never overwritten.",
    )
    parser.add_argument(
        "--user",
        dest="user_emails",
        action="append",
        default=[],
        help="User email to export. Repeat for a batch; omit for all users.",
    )
    parser.add_argument(
        "--start-date",
        type=_calendar_date,
        help="Inclusive session creation date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--end-date",
        type=_calendar_date,
        help="Inclusive session creation date in YYYY-MM-DD format.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = UserRecordExportService()
    selection = ExportSelection.from_dates(
        user_emails=args.user_emails,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    if args.output is None:
        export_directory = PROJECT_ROOT / "data" / "exports"
        export_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(export_directory, 0o700)
        output_path = export_directory / service.default_filename()
    else:
        output_path = args.output.expanduser()

    artifact = service.export_to(
        output_path,
        audit_context=ExportAuditContext(
            actor_email=args.requested_by,
            ip_address="cli",
            user_agent="scripts/export_user_content.py",
        ),
        selection=selection,
        validate_admin=True,
    )
    print(
        json.dumps(
            {
                "event": "user_content_export_completed",
                "path": str(artifact.path.resolve()),
                "filename": artifact.filename,
                "generated_at": artifact.generated_at.isoformat(),
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
                "counts": artifact.counts,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

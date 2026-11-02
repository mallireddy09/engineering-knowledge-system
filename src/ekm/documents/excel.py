"""Generate a new Excel dashboard with safe user cells and derived analytics."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from openpyxl import Workbook
from openpyxl.chart import BarChart, PieChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo

if TYPE_CHECKING:
    from ekm.models import Record

_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_COLUMNS = {
    "Tasks": (
        "task",
        (
            "id",
            "title",
            "project_id",
            "status",
            "technologies",
            "created_at",
            "completed_at",
            "lessons_learned",
        ),
    ),
    "Projects": (
        "project",
        ("id", "title", "repository", "description", "technologies", "services", "updated_at"),
    ),
    "Decisions": (
        "decision",
        (
            "id",
            "title",
            "project_id",
            "chosen_approach",
            "rationale",
            "alternatives",
            "trade_offs",
            "status",
        ),
    ),
    "Debugging": (
        "debugging",
        (
            "id",
            "title",
            "project_id",
            "symptoms",
            "root_cause",
            "resolution",
            "prevention_strategy",
            "technologies",
        ),
    ),
    "Technical Debt": (
        "debt",
        (
            "id",
            "title",
            "project_id",
            "priority",
            "severity",
            "status",
            "proposed_solution",
            "target_date",
        ),
    ),
    "Learning Progress": (
        "question",
        (
            "id",
            "question",
            "topic",
            "technology",
            "difficulty",
            "review_count",
            "correct_count",
            "confidence_score",
            "next_review_at",
        ),
    ),
    "Review History": (
        "review",
        ("id", "question_id", "review_date", "user_answer", "score", "feedback", "next_review_at"),
    ),
}


def _safe_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(value, list):
        value = "; ".join(str(item) for item in value)
    elif isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return _CONTROLS.sub("", str(value))[:32767]


def _table(sheet: object, name: str, columns: tuple[str, ...], rows: list[list[object]]) -> None:
    sheet.append([column.replace("_", " ").title() for column in columns])
    for row in rows or [[None] * len(columns)]:
        sheet.append([_safe_value(value) for value in row])
    # Treat every external string as literal text, including =, +, -, and @ prefixes.
    for row in sheet.iter_rows():
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = "s"
            if isinstance(cell.value, datetime):
                cell.number_format = 'yyyy-mm-dd hh:mm "UTC"'
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    table = Table(displayName=name, ref=sheet.dimensions)
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    sheet.add_table(table)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False
    for column in sheet.columns:
        letter = column[0].column_letter
        sheet.column_dimensions[letter].width = min(48, max(18, len(str(column[0].value)) + 3))
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="183B4E")


def write_excel(records: list[Record], path: Path) -> Path:
    """Write a new snapshot; refresh by exporting to another destination."""
    path = Path(path)
    book = Workbook()
    overview = book.active
    overview.title = "Overview"
    overview.append(["Engineering knowledge dashboard", "Value"])
    overview.append(["Generated UTC", datetime.now(timezone.utc).replace(tzinfo=None)])
    overview["B2"].number_format = 'yyyy-mm-dd hh:mm "UTC"'
    overview.append(["Records", len(records)])
    for sheet_name, (kind, columns) in _COLUMNS.items():
        sheet = book.create_sheet(sheet_name)
        selected = [
            record for record in records if getattr(record.kind, "value", record.kind) == kind
        ]
        rows = [[getattr(record, column, None) for column in columns] for record in selected]
        _table(sheet, "EKM" + sheet_name.replace(" ", ""), columns, rows)
    tasks = [record for record in records if getattr(record.kind, "value", record.kind) == "task"]
    statuses = Counter(str(getattr(record, "status", "unknown")) for record in tasks)
    status_rows = [[status, count] for status, count in sorted(statuses.items())]
    overview.append(["Task status", "Tasks"])
    for row in status_rows or [["No tasks", 0]]:
        overview.append([_safe_value(value) for value in row])
        # These labels are external data, including the status string.
        overview.cell(overview.max_row, 1).data_type = "s"
    status_end = overview.max_row
    chart = BarChart()
    chart.title = "Task completion and work in progress"
    chart.y_axis.title = "Tasks"
    chart.add_data(
        Reference(overview, min_col=2, min_row=4, max_row=status_end), titles_from_data=True
    )
    chart.set_categories(Reference(overview, min_col=1, min_row=5, max_row=status_end))
    overview.add_chart(chart, "D2")
    metrics_start = status_end + 3
    metrics = [
        (
            "Completed tasks",
            '=COUNTIF(EKMTasks[Status],"completed")+COUNTIF(EKMTasks[Status],"Done")',
        ),
        ("Reviews completed", "=COUNT(EKMReviewHistory[Score])"),
        ("Average quiz score", "=IFERROR(AVERAGE(EKMReviewHistory[Score]),0)"),
        (
            "Knowledge gaps (< 0.6 confidence)",
            '=COUNTIFS(EKMLearningProgress[Confidence Score],"<0.6",EKMLearningProgress[Question],"<>")',
        ),
        ("Open technical debt", '=COUNTIF(EKMTechnicalDebt[Status],"open")'),
    ]
    for row_number, (label, formula) in enumerate(metrics, metrics_start):
        overview.cell(row_number, 1, label)
        overview.cell(row_number, 2, formula)
    for cell in (overview.cell(metrics_start + 2, 2),):
        cell.number_format = "0.0%"
    trends = book.create_sheet("Technology Trends")
    technology = Counter(
        tech for record in records for tech in set(getattr(record, "technologies", []))
    )
    _table(
        trends,
        "EKMTechnologyTrends",
        ("technology", "record_count"),
        [[key, value] for key, value in technology.most_common()],
    )
    if technology:
        pie = PieChart()
        pie.title = "Technology distribution"
        pie.add_data(
            Reference(trends, min_col=2, min_row=1, max_row=len(technology) + 1),
            titles_from_data=True,
        )
        pie.set_categories(Reference(trends, min_col=1, min_row=2, max_row=len(technology) + 1))
        trends.add_chart(pie, "D2")
    # Activity, recurring investigations and monthly completion trends are reproducible snapshots.
    insights_start = metrics_start + len(metrics) + 3
    activities = Counter(
        str(getattr(record, "project_id", None) or "Unassigned") for record in records
    )
    overview.cell(insights_start, 1, "Project activity")
    overview.cell(insights_start, 2, "Records")
    cursor = insights_start + 1
    for label, count in activities.most_common():
        overview.cell(cursor, 1, _safe_value(label)).data_type = "s"
        overview.cell(cursor, 2, count)
        cursor += 1
    cursor += 2
    overview.cell(cursor, 1, "Recurring debugging symptoms")
    overview.cell(cursor, 2, "Investigations")
    debugging = [
        record for record in records if getattr(record.kind, "value", record.kind) == "debugging"
    ]
    for label, count in Counter(
        _safe_value(getattr(record, "symptoms", "")) for record in debugging
    ).most_common():
        cursor += 1
        overview.cell(cursor, 1, label).data_type = "s"
        overview.cell(cursor, 2, count)
    cursor += 3
    overview.cell(cursor, 1, "Completed month (UTC)")
    overview.cell(cursor, 2, "Tasks")
    months = Counter(
        record.completed_at.astimezone(timezone.utc).strftime("%Y-%m")
        for record in tasks
        if getattr(record, "completed_at", None)
    )
    for month, count in sorted(months.items()):
        cursor += 1
        overview.cell(cursor, 1, month)
        overview.cell(cursor, 2, count)
    overview.freeze_panes = "A2"
    overview.sheet_view.showGridLines = False
    overview.column_dimensions["A"].width = 45
    overview.column_dimensions["B"].width = 24
    for cell in overview[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="183B4E")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        try:
            book.save(output)
        except Exception:
            output.close()
            path.unlink(missing_ok=True)
            raise
    return path

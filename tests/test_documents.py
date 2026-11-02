import os
import stat
from datetime import datetime, timezone
from uuid import uuid4
from zipfile import ZipFile

import pytest
from docx import Document
from openpyxl import load_workbook

from ekm.documents import write_excel, write_word
from ekm.models import Kind, Record


@pytest.fixture
def records():
    return [
        Record(
            kind=Kind.task,
            title="Cache invalidation",
            project_id="platform",
            status="completed",
            technologies=["Python"],
            completed_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
            description="Version conflicts",
            implementation_summary="Compare the expected version",
            lessons_learned=["Reject stale writes"],
        ),
        Record(
            kind=Kind.question,
            title="Versioning review",
            question="Why reject stale writes?",
            expected_answer="Prevent lost updates",
            topic="concurrency",
            technologies=["Python"],
        ),
        Record(
            kind=Kind.review,
            title="Review result",
            question_id=uuid4(),
            user_answer="Avoid a lost update",
            score=0.8,
            feedback="Correct mechanism",
        ),
    ]


def test_word_contains_records_metadata_and_page_number(tmp_path, records):
    output = write_word(records, tmp_path / "journal.docx")
    document = Document(output)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "Cache invalidation" in text
    assert "Compare the expected version" in text
    assert "Reject stale writes" in text
    assert any(
        "platform" in cell.text
        for table in document.tables
        for row in table.rows
        for cell in row.cells
    )
    with ZipFile(output) as archive:
        assert b"PAGE" in archive.read("word/footer1.xml")


@pytest.mark.parametrize("export,extension", [(write_word, "docx"), (write_excel, "xlsx")])
def test_exports_refuse_to_overwrite_unrelated_file(tmp_path, records, export, extension):
    path = tmp_path / f"existing.{extension}"
    path.write_bytes(b"precious user data")
    with pytest.raises(FileExistsError):
        export(records, path)
    assert path.read_bytes() == b"precious user data"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
@pytest.mark.parametrize("export,extension", [(write_word, "docx"), (write_excel, "xlsx")])
def test_exports_are_owner_only_with_permissive_umask(tmp_path, records, export, extension):
    previous = os.umask(0)
    try:
        path = export(records, tmp_path / f"private.{extension}")
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_excel_has_requested_sheets_tables_charts_and_formulas(tmp_path, records):
    output = write_excel(records, tmp_path / "dashboard.xlsx")
    workbook = load_workbook(output)
    assert workbook.sheetnames == [
        "Overview",
        "Tasks",
        "Projects",
        "Decisions",
        "Debugging",
        "Technical Debt",
        "Learning Progress",
        "Review History",
        "Technology Trends",
    ]
    assert workbook["Tasks"]["B2"].value == "Cache invalidation"
    assert workbook["Tasks"]["G2"].value == datetime(2026, 10, 15)
    assert workbook["Tasks"].freeze_panes == "A2"
    assert "EKMTasks" in workbook["Tasks"].tables
    assert len(workbook["Overview"]._charts) == 1
    assert len(workbook["Technology Trends"]._charts) == 1
    formulas = [cell.value for row in workbook["Overview"] for cell in row if cell.data_type == "f"]
    assert any("COUNTIF(EKMTasks" in formula for formula in formulas)
    assert any("AVERAGE(EKMReviewHistory" in formula for formula in formulas)


def test_excel_external_strings_cannot_inject_formulas(tmp_path):
    records = [
        Record(
            kind=Kind.task,
            title='=HYPERLINK("https://example.com", "click")',
            project_id="=1+2",
            status="=1+2",
            technologies=["=1+2"],
        )
    ]
    output = write_excel(records, tmp_path / "safe.xlsx")
    workbook = load_workbook(output)
    assert workbook["Tasks"]["B2"].value.startswith("=HYPERLINK")
    assert workbook["Tasks"]["B2"].data_type == "s"
    assert workbook["Tasks"]["C2"].data_type == "s"
    assert workbook["Tasks"]["D2"].data_type == "s"
    assert workbook["Technology Trends"]["A2"].data_type == "s"
    assert workbook["Overview"]["A5"].data_type == "s"


def test_empty_exports_are_valid(tmp_path):
    assert len(Document(write_word([], tmp_path / "empty.docx")).paragraphs) > 0
    workbook = load_workbook(write_excel([], tmp_path / "empty.xlsx"))
    assert workbook["Overview"]["B3"].value == 0


def test_word_only_creates_safe_web_hyperlinks(tmp_path):
    records = [
        Record(
            kind=Kind.task,
            title="References",
            follow_up_actions=[
                "https://example.com/design",
                "file:///etc/passwd",
                "https://user:password@example.com",
            ],
        )
    ]
    with ZipFile(write_word(records, tmp_path / "links.docx")) as archive:
        relations = archive.read("word/_rels/document.xml.rels")
    assert b'Target="https://example.com/design"' in relations
    assert b'Target="file:///etc/passwd"' not in relations
    assert b'Target="https://user:password@example.com"' not in relations

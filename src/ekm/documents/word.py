"""Generate a journal without changing an existing document."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

if TYPE_CHECKING:
    from ekm.models import Record

_XML_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_METADATA = ("id", "project_id", "task_id", "repository", "branch", "status", "source_agent")
_SKIP = {"kind", "title", "created_at", "updated_at", "content_hash", "schema_version", *_METADATA}
_TITLES = {
    "description": "Executive summary",
    "implementation_summary": "Implementation details",
    "technical_decisions": "Architecture and decisions",
    "debugging_references": "Debugging journey",
    "tests_performed": "Testing",
    "follow_up_actions": "Follow-up actions",
    "expected_answer": "Review answer",
}
_COMMON = {
    "description",
    "technologies",
    "affected_components",
    "related_task_ids",
    "verified_facts",
    "inferences",
    "unanswered_questions",
    "references",
    "source_session",
    "completed_at",
}
_BY_KIND = {
    "task": {
        "pull_request_id",
        "commit_ids",
        "problem_statement",
        "requirements",
        "implementation_summary",
        "files_changed",
        "tests_performed",
        "technical_decisions",
        "debugging_references",
        "lessons_learned",
        "mistakes",
        "improvements",
        "follow_up_actions",
    },
    "decision": {
        "context",
        "problem",
        "chosen_approach",
        "alternatives",
        "rejected_alternatives",
        "rationale",
        "trade_offs",
        "consequences",
        "reversibility",
        "review_date",
    },
    "question": {
        "question",
        "expected_answer",
        "explanation",
        "difficulty",
        "technology",
        "topic",
        "question_source",
        "review_schedule",
        "next_review_at",
        "review_count",
        "correct_count",
        "confidence_score",
    },
    "review": {
        "question_id",
        "user_answer",
        "score",
        "feedback",
        "evaluator",
        "review_date",
        "next_review_at",
    },
    "debugging": {
        "symptoms",
        "environment",
        "error_messages",
        "investigation_timeline",
        "hypotheses",
        "tests_performed",
        "incorrect_assumptions",
        "failed_approaches",
        "root_cause",
        "resolution",
        "prevention_strategy",
        "monitoring_recommendations",
    },
    "debt": {
        "category",
        "priority",
        "severity",
        "impact",
        "workaround",
        "proposed_solution",
        "owner",
        "target_date",
    },
    "project": {"name", "services", "dependencies", "architecture_summary"},
}


def _text(value: object) -> str:
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return _XML_CONTROLS.sub("", str(value))


def _hyperlink(paragraph: object, value: str) -> bool:
    """Allow web references only; never emit executable or local-file links."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
        return False
    rel = paragraph.part.relate_to(  # type: ignore[attr-defined]
        value,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rel)
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "176B91")
    props.append(color)
    run.append(props)
    text = OxmlElement("w:t")
    text.text = _text(value)
    run.append(text)
    link.append(run)
    paragraph._p.append(link)  # type: ignore[attr-defined]
    return True


def write_word(records: list[Record], path: Path) -> Path:
    """Write a new DOCX file; an existing destination raises FileExistsError."""
    path = Path(path)
    document = Document()
    section = document.sections[0]
    section.top_margin = section.bottom_margin = Inches(0.7)
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10)
    normal.paragraph_format.space_after = Pt(6)
    for name in ("Title", "Heading 1", "Heading 2"):
        document.styles[name].font.color.rgb = RGBColor.from_string("183B4E")
    section.header.paragraphs[0].text = "EKM  |  Engineering knowledge journal"
    footer = section.footer.paragraphs[0]
    footer.add_run("Engineering knowledge  •  Page ")
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    document.add_heading("Engineering knowledge journal", 0)
    document.add_paragraph(f"{len(records)} knowledge record(s)", style="Subtitle")
    if not records:
        document.add_paragraph("No knowledge records were selected for this journal.")
    for index, record in enumerate(records):
        if index:
            document.add_page_break()
        data = record.model_dump(mode="json")
        document.add_heading(_text(record.title), 1)
        kind = getattr(record.kind, "value", record.kind)
        document.add_paragraph(_text(kind).replace("_", " ").title(), style="Subtitle")
        table = document.add_table(rows=0, cols=2)
        table.style = "Light Shading Accent 1"
        for key in (*_METADATA, "created_at", "updated_at"):
            value = data.get(key)
            if value in (None, "", []):
                continue
            row = table.add_row().cells
            row[0].text = key.replace("_", " ").title()
            row[1].text = _text(value)
        for key, value in data.items():
            if (
                key in _SKIP
                or key not in _COMMON | _BY_KIND.get(kind, set())
                or value in (None, "", [], {})
            ):
                continue
            document.add_heading(_TITLES.get(key, key.replace("_", " ").title()), 2)
            values = value if isinstance(value, list) else [value]
            for item in values:
                paragraph = document.add_paragraph(
                    style="List Bullet" if isinstance(value, list) else None
                )
                rendered = _text(item)
                if not _hyperlink(paragraph, rendered):
                    paragraph.add_run(rendered)
    document.core_properties.title = "Engineering knowledge journal"
    document.core_properties.subject = "Approved engineering knowledge records"
    document.core_properties.author = "EKM"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids a check-then-save race and protects unrelated user data.
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        try:
            document.save(output)
        except Exception:
            output.close()
            path.unlink(missing_ok=True)
            raise
    return path

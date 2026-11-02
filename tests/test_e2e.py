"""One complete local workflow through the installed CLI's entry function."""

import json
from uuid import uuid4

from docx import Document
from openpyxl import load_workbook

from ekm.cli import main


def test_engineering_memory_workflow(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EKM_HOME", str(tmp_path / "private"))

    def run(*args):
        capsys.readouterr()
        assert main(list(args)) == 0
        return capsys.readouterr().out

    run("account", "add", "demo", "--type", "mock")
    run("account", "switch", "demo", "--approve")
    run("init", "--approve")
    run("check")
    question_id = str(uuid4())
    task_id = str(uuid4())
    entries = [
        {
            "kind": "project",
            "title": "Event platform",
            "name": "events",
            "architecture_summary": "Bounded consumer workers",
        },
        {
            "id": task_id,
            "kind": "task",
            "title": "Bound retries",
            "task_id": "DEMO-1",
            "implementation_summary": "Capped retries",
            "lessons_learned": ["Bound retry pressure"],
        },
        {
            "kind": "decision",
            "title": "Use backpressure",
            "related_task_ids": [task_id],
            "chosen_approach": "Pause consumption",
            "rationale": "Prevent memory exhaustion",
        },
        {
            "kind": "debugging",
            "title": "Consumer timeout",
            "related_task_ids": [task_id],
            "symptoms": ["Kafka lag"],
            "root_cause": "Pool exhausted",
            "resolution": "Bound workers",
        },
        {"kind": "debt", "title": "Add stress test", "priority": "high", "status": "open"},
        {
            "id": question_id,
            "kind": "question",
            "title": "Backpressure mechanism",
            "question": "Why pause?",
            "expected_answer": "Bound memory",
            "topic": "reliability",
        },
    ]
    for entry in entries:
        entry.update(project_id="events", technologies=["Kafka"])
        path = tmp_path / f"{entry['kind']}.json"
        path.write_text(json.dumps(entry))
        run("import", "--input", str(path), "--approve")
    assert "Consumer timeout" in run("debug", "search", "timeout")
    assert "Use backpressure" in run("decision", "list")
    assert "Add stress test" in run("debt", "--priority", "high")
    assert "record_count" in run("project", "events")
    related = json.loads(run("show", "task", task_id, "--related"))
    assert len(related["related"]) == 2
    assert "Bound memory" not in run("quiz", "--topic", "reliability")
    run(
        "review",
        "--question",
        question_id,
        "--answer",
        "Bound memory",
        "--score",
        "1",
        "--feedback",
        "Correct mechanism",
        "--approve",
    )
    assert json.loads(run("quiz"))[0]["review_count"] == 1
    assert json.loads(run("weekly"))["review_count"] == 1
    word = tmp_path / "journal.docx"
    excel = tmp_path / "dashboard.xlsx"
    run("export", "--format", "word", "--output", str(word))
    run("export", "--format", "excel", "--output", str(excel))
    assert "Bound retries" in "\n".join(p.text for p in Document(word).paragraphs)
    workbook = load_workbook(excel)
    assert workbook["Learning Progress"]["F2"].value == 1
    assert workbook["Learning Progress"]["G2"].value == 1
    backup = tmp_path / "backup.json"
    run("backup", "--output", str(backup))
    run("account", "add", "recovery", "--type", "mock")
    run("account", "switch", "recovery", "--approve")
    run("init", "--approve")
    run("restore", "--input", str(backup), "--offline", "--approve")
    assert json.loads(run("search", "Kafka")) == []
    assert len(json.loads(run("pending"))) == 7
    run("sync", "--approve")
    assert json.loads(run("insights"))["record_count"] == 7
    assert json.loads(run("quiz"))[0]["review_count"] == 1

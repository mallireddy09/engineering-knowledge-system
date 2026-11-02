"""Native Google journals and dashboards using EKM's existing artifact models.

Docs updates append revision-guarded snapshots. Sheets updates only add new tabs,
since Sheets has no requiredRevisionId equivalent for replacing existing cells.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory, mkstemp
from typing import TYPE_CHECKING

from docx import Document
from docx.table import Table
from filelock import FileLock
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from ekm.documents.excel import write_excel
from ekm.documents.word import write_word
from ekm.models import Kind, Record
from ekm.providers.base import ConflictError, StorageError, require_approval

if TYPE_CHECKING:
    from ekm.providers.google_drive import GoogleDriveProvider

DRIVE = "https://www.googleapis.com/drive/v3/files"
DOCS = "https://docs.googleapis.com/v1/documents"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
_ID = re.compile(r"[A-Za-z0-9_-]+")
_TABLE_COLUMN = re.compile(r"(EKM[A-Za-z]+)\[([^]]+)\]")


def _id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise StorageError("Google returned an invalid native file identifier.")
    return value


def _query_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _digest(records: list[Record], native_id: str | None = None) -> str:
    payload = []
    for record in sorted(records, key=lambda item: str(item.id)):
        data = record.model_dump(mode="json")
        # Attaching the generated journal reference must not cause another journal.
        data.pop("content_hash", None)
        data.pop("updated_at", None)
        if native_id:
            data["references"] = [
                item
                for item in data["references"]
                if item
                not in {
                    f"google-doc-id:{native_id}",
                    f"https://docs.google.com/document/d/{native_id}/edit",
                }
            ]
        payload.append(data)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _units(value: str) -> int:
    """Google Docs indexes are UTF-16 code units, including astral characters."""
    return len(value.encode("utf-16-le")) // 2


def _journal_blocks(records: list[Record]) -> list[tuple[str, str | None]]:
    with TemporaryDirectory(prefix="ekm-google-doc-") as temporary:
        path = write_word(records, Path(temporary) / "journal.docx")
        document = Document(path)
        result = []
        for block in document.iter_inner_content():
            if isinstance(block, Table):
                result.extend(
                    (" | ".join(cell.text for cell in row.cells), None) for row in block.rows
                )
            else:
                style = {"Title": "TITLE", "Heading 1": "HEADING_1", "Heading 2": "HEADING_2"}.get(
                    block.style.name
                )
                text = block.text
                if block.style.name == "List Bullet":
                    text = "• " + text
                if text:
                    result.append((text, style))
        return result


class GoogleDocuments:
    def __init__(self, provider: GoogleDriveProvider):
        self.provider = provider
        self.client = provider.client

    def _native(
        self,
        *,
        role: str,
        folder: str,
        mime: str,
        key: str,
        title: str,
        pinned_id: str | None = None,
    ) -> tuple[str, str]:
        directory = self.provider.state_dir
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(str(directory / ".google-native.lock")):
            return self._native_locked(
                role=role, folder=folder, mime=mime, key=key, title=title, pinned_id=pinned_id
            )

    def _native_locked(
        self, *, role: str, folder: str, mime: str, key: str, title: str, pinned_id: str | None
    ) -> tuple[str, str]:
        parent = self.provider.ensure_folder(folder, approved=True)
        state_path = self.provider.state_dir / "google-native-operations.json"
        try:
            operations = json.loads(state_path.read_text()) if state_path.exists() else {}
            if not isinstance(operations, dict):
                raise ValueError
        except (ValueError, OSError) as error:
            raise StorageError(
                "Google native operation journal is invalid; retain it for recovery."
            ) from error
        operation_key = hashlib.sha256(f"{parent}:{role}:{key}".encode()).hexdigest()

        def remember(value: dict | None) -> None:
            if value is None:
                operations.pop(operation_key, None)
            else:
                operations[operation_key] = value
            descriptor, temporary = mkstemp(dir=state_path.parent, prefix=".google-native-")
            try:
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(operations, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, state_path)
            finally:
                Path(temporary).unlink(missing_ok=True)

        if pinned_id:
            item = self.provider.verify_file(_id(pinned_id), role, parent_id=parent)
            if item.get("mimeType") != mime:
                raise StorageError("Pinned native file has an incompatible Google format.")
            return pinned_id, parent
        properties = self.provider.owner_properties(
            role, **({"task": key} if role == "journal" else {"key": key})
        )
        query = [f"'{_query_string(parent)}' in parents", "trashed = false"]
        query.extend(
            "appProperties has { key='"
            + _query_string(name)
            + "' and value='"
            + _query_string(value)
            + "' }"
            for name, value in properties.items()
        )
        matches = self.client.iter_files(" and ".join(query))
        if len(matches) > 1:
            raise ConflictError("Multiple app-owned native files match; reconcile them explicitly.")
        if matches:
            file_id = _id(matches[0]["id"])
            item = self.provider.verify_file(file_id, role, parent_id=parent)
            if item.get("mimeType") != mime:
                raise StorageError("App-owned native file has an incompatible Google format.")
            remember({"id": file_id})
            return file_id, parent
        previous = operations.get(operation_key)
        if previous:
            if not isinstance(previous, dict):
                raise StorageError("Google native operation entry is invalid.")
            if previous.get("id"):
                file_id = _id(previous["id"])
                item = self.provider.verify_file(file_id, role, parent_id=parent)
                if item.get("mimeType") != mime or item.get("appProperties") != properties:
                    raise StorageError(
                        "Tracked Google native file has incompatible ownership or type."
                    )
                return file_id, parent
            raise StorageError(
                "Prior native-file creation has an unknown outcome. Retry discovery after Google recovers; "
                "if no tagged file exists, inspect the native operation journal before explicitly clearing its pending entry."
            )
        # Native Docs and Sheets do not support Drive pre-generated IDs. A tagged
        # create is reconciled by this query after an ambiguous network outcome.
        remember({"pending": True})
        try:
            item = self.client.request(
                "POST",
                DRIVE,
                json={
                    "name": title[:200],
                    "mimeType": mime,
                    "parents": [parent],
                    "appProperties": properties,
                },
                params={"fields": "id,name,mimeType,parents,appProperties"},
            ).json()
        except StorageError as error:
            if getattr(error, "status_code", None) in {400, 401, 403, 404}:
                remember(None)  # A definitive rejection did not create a file.
            raise
        file_id = _id(item["id"])
        remember({"id": file_id})
        matches = self.client.iter_files(" and ".join(query))
        if len(matches) > 1:
            raise ConflictError("Concurrent native file creation requires explicit reconciliation.")
        self.provider.verify_file(file_id, role, parent_id=parent)
        return file_id, parent

    def journal(
        self, task: Record, related: list[Record] | None = None, *, approved: bool = False
    ) -> dict:
        require_approval(approved)
        if task.kind != Kind.task:
            raise ValueError("Google journals require a task record")
        file_id, _ = self._native(
            role="journal",
            folder="journals",
            mime=DOC_MIME,
            key=str(task.id),
            title=f"{task.task_id or task.id} — {task.title}",
        )
        records = [task, *(related or [])]
        digest = _digest(records, file_id)
        marker = "EKM_" + digest
        document = self.client.request("GET", f"{DOCS}/{file_id}").json()
        result = {
            "id": file_id,
            "url": f"https://docs.google.com/document/d/{file_id}/edit",
            "task_id": str(task.id),
            "revision_id": document.get("revisionId"),
        }
        if marker in document.get("namedRanges", {}):
            return {**result, "updated": False}
        revision = document.get("revisionId")
        content = document.get("body", {}).get("content", [])
        if not revision or not content:
            raise StorageError("Google Docs omitted the body or revision; no content was changed.")
        end_index = max(element.get("endIndex", 0) for element in content) - 1
        if end_index < 1:
            raise StorageError("Google Docs returned an invalid append position.")
        blocks = _journal_blocks(records)
        text = "\n" + "\n".join(value for value, _ in blocks) + "\n"
        requests = [{"insertText": {"location": {"index": end_index}, "text": text}}]
        cursor = end_index + 1
        for value, style in blocks:
            end = cursor + _units(value) + 1
            if style:
                requests.append(
                    {
                        "updateParagraphStyle": {
                            "range": {"startIndex": cursor, "endIndex": end},
                            "paragraphStyle": {"namedStyleType": style},
                            "fields": "namedStyleType",
                        }
                    }
                )
            cursor = end
        requests.append(
            {
                "createNamedRange": {
                    "name": marker,
                    "range": {"startIndex": end_index, "endIndex": end_index + _units(text)},
                }
            }
        )
        response = self.client.request(
            "POST",
            f"{DOCS}/{file_id}:batchUpdate",
            json={
                "requests": requests,
                "writeControl": {"requiredRevisionId": revision},
            },
        ).json()
        result["revision_id"] = response.get("writeControl", {}).get("requiredRevisionId", revision)
        return {**result, "updated": True}

    def dashboard(
        self, records: list[Record], spreadsheet_id: str | None = None, *, approved: bool = False
    ) -> dict:
        require_approval(approved)
        file_id, _ = self._native(
            role="dashboard",
            folder="dashboards",
            mime=SHEET_MIME,
            key="primary",
            title="EKM engineering dashboard",
            pinned_id=spreadsheet_id,
        )
        digest = _digest(records)
        existing = self.client.request(
            "GET",
            f"{SHEETS}/{file_id}",
            params={"fields": "spreadsheetId,sheets(properties),developerMetadata"},
        ).json()
        result = {
            "id": file_id,
            "url": f"https://docs.google.com/spreadsheets/d/{file_id}/edit",
            "snapshot": digest,
        }
        for marker in existing.get("developerMetadata", []):
            if (
                marker.get("metadataKey") == "ekm_snapshot"
                and marker.get("metadataValue") == digest
            ):
                return {**result, "updated": False, "sheet_ids": []}
        with TemporaryDirectory(prefix="ekm-google-sheet-") as temporary:
            path = write_excel(records, Path(temporary) / "dashboard.xlsx")
            book = load_workbook(path)
            requests, sheet_ids = _sheet_requests(book, existing, digest)
            self.client.request(
                "POST",
                f"{SHEETS}/{file_id}:batchUpdate",
                json={"requests": requests, "includeSpreadsheetInResponse": False},
            )
            book.close()
        return {
            **result,
            "updated": True,
            "sheet_ids": sheet_ids,
            "refresh_mode": "new snapshot tabs; existing tabs preserved",
        }


def _sheet_requests(book, existing: dict, digest: str) -> tuple[list[dict], list[int]]:
    used_ids = {sheet["properties"]["sheetId"] for sheet in existing.get("sheets", [])}
    used_titles = {sheet["properties"]["title"] for sheet in existing.get("sheets", [])}
    ids, titles = {}, {}
    suffix = secrets.token_hex(4)
    for sheet in book:
        identifier = secrets.randbelow(2_000_000_000) + 1
        while identifier in used_ids:
            identifier = secrets.randbelow(2_000_000_000) + 1
        used_ids.add(identifier)
        ids[sheet.title] = identifier
        titles[sheet.title] = (
            sheet.title if sheet.title not in used_titles else f"{sheet.title} [{suffix}]"
        )
    columns = {}
    for sheet in book:
        for table in sheet.tables.values():
            for offset, column in enumerate(table.tableColumns, 1):
                columns[(table.displayName, column.name)] = (
                    "'"
                    + titles[sheet.title].replace("'", "''")
                    + "'!"
                    + f"{get_column_letter(offset)}2:{get_column_letter(offset)}{sheet.max_row}"
                )

    def cell_value(cell) -> dict:
        value = cell.value
        if value is None:
            return {}
        if cell.data_type == "f":

            def replace(match):
                key = match.group(1), match.group(2)
                if key not in columns:
                    raise StorageError("Dashboard formula references an unsupported table column.")
                return columns[key]

            return {"formulaValue": _TABLE_COLUMN.sub(replace, value)}
        if isinstance(value, bool):
            return {"boolValue": value}
        if isinstance(value, (int, float)):
            return {"numberValue": value}
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        return {"stringValue": str(value)}

    requests = []
    for sheet in book:
        identifier = ids[sheet.title]
        requests.append(
            {
                "addSheet": {
                    "properties": {
                        "sheetId": identifier,
                        "title": titles[sheet.title],
                        "gridProperties": {
                            "rowCount": max(100, sheet.max_row),
                            "columnCount": max(26, sheet.max_column),
                            "frozenRowCount": 1,
                        },
                    }
                }
            }
        )
        rows = []
        for row in sheet:
            rows.append(
                {
                    "values": [
                        {
                            "userEnteredValue": cell_value(cell),
                            "userEnteredFormat": {"wrapStrategy": "WRAP"},
                        }
                        for cell in row
                    ]
                }
            )
        requests.append(
            {
                "updateCells": {
                    "start": {"sheetId": identifier},
                    "rows": rows,
                    "fields": "userEnteredValue,userEnteredFormat.wrapStrategy",
                }
            }
        )
        requests.append(
            {
                "repeatCell": {
                    "range": {"sheetId": identifier, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 0.09, "green": 0.23, "blue": 0.31},
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                            },
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat",
                }
            }
        )
        requests.append(
            {
                "setBasicFilter": {
                    "filter": {
                        "range": {
                            "sheetId": identifier,
                            "startRowIndex": 0,
                            "endRowIndex": sheet.max_row,
                            "startColumnIndex": 0,
                            "endColumnIndex": sheet.max_column,
                        }
                    }
                }
            }
        )
        requests.append(
            {
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": identifier,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": sheet.max_column,
                    }
                }
            }
        )
        for chart in sheet._charts:
            request = _chart_request(chart, identifier, sheet.max_row)
            if request:
                requests.append(request)
    requests.append(
        {
            "createDeveloperMetadata": {
                "developerMetadata": {
                    "metadataKey": "ekm_snapshot",
                    "metadataValue": digest,
                    "location": {"spreadsheet": True},
                    "visibility": "DOCUMENT",
                }
            }
        }
    )
    return requests, list(ids.values())


def _chart_request(chart, sheet_id: int, row_count: int) -> dict | None:
    # Reuse each chart's source ranges from the existing Excel chart model.
    if not chart.series:
        return None
    series = chart.series[0]
    numeric = getattr(getattr(series, "val", None), "numRef", None)
    category = getattr(series, "cat", None)
    category_ref = getattr(category, "numRef", None) or getattr(category, "strRef", None)
    if numeric is None or category_ref is None:
        return None

    def source(formula: str) -> dict:
        from openpyxl.utils.cell import range_boundaries

        _, coordinates = formula.rsplit("!", 1)
        start_col, start_row, end_col, end_row = range_boundaries(coordinates)
        return {
            "sheetId": sheet_id,
            "startRowIndex": start_row - 1,
            "endRowIndex": min(end_row, row_count),
            "startColumnIndex": start_col - 1,
            "endColumnIndex": end_col,
        }

    domain = {"sourceRange": {"sources": [source(category_ref.f)]}}
    data = {"sourceRange": {"sources": [source(numeric.f)]}}
    spec = {
        "title": "Technology distribution"
        if chart.__class__.__name__ == "PieChart"
        else "Task completion and work in progress"
    }
    if chart.__class__.__name__ == "PieChart":
        spec["pieChart"] = {"legendPosition": "RIGHT_LEGEND", "domain": domain, "series": data}
    else:
        spec["basicChart"] = {
            "chartType": "COLUMN",
            "legendPosition": "NO_LEGEND",
            "headerCount": 0,
            "domains": [{"domain": domain}],
            "series": [{"series": data, "targetAxis": "LEFT_AXIS"}],
        }
    return {
        "addChart": {
            "chart": {
                "spec": spec,
                "position": {
                    "overlayPosition": {
                        "anchorCell": {"sheetId": sheet_id, "rowIndex": 1, "columnIndex": 3}
                    }
                },
            }
        }
    }

"""Google Drive proxy resource.

Wraps `/api/v1/drive/list`, `/api/v1/drive/read`, `/api/v1/drive/upload`.
The Drive OAuth token lives server-side and never leaves it — the SDK
just describes what it wants and gets bytes or metadata back.

Drive must be CONNECTED on the target workspace (via workspace settings
in the UI). When the workspace doesn't have a connection or the
connection lacks the Drive scope, `DriveNotConnected` is raised.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import List, Optional, Union

from .._transport import AsyncTransport, SyncTransport
from .dispatch import _resolve_idempotency_key


# ─── Dataclasses ─────────────────────────────────────────────────────────


@dataclass
class DriveFile:
    """A single entry returned by `drive.list_folder` / `drive.list_path`."""

    id: str
    name: str
    mime_type: str
    is_folder: bool


@dataclass
class DriveListResult:
    folder_id: str
    files: List[DriveFile]
    next_page_token: Optional[str]


@dataclass
class DriveDownloadResult:
    """Result of `drive.read_file`.

    `content` is the raw bytes. For Google-native docs (Docs / Sheets /
    Slides) the server exports as PDF — `exported=True` and
    `mime_type="application/pdf"`. Inspect `original_mime_type` to see
    the source format.
    """

    content: bytes
    file_id: str
    mime_type: str
    original_mime_type: str
    exported: bool
    file_name: str


@dataclass
class DriveUploadResult:
    drive_file_id: str
    name: str
    parent_folder_id: str
    web_view_link: Optional[str]


# ─── Helpers ─────────────────────────────────────────────────────────────


def _ensure_bytes(content: Union[bytes, bytearray, memoryview]) -> bytes:
    """Coerce assorted bytes-ish inputs into `bytes` for base64 encoding."""
    if isinstance(content, bytes):
        return content
    if isinstance(content, (bytearray, memoryview)):
        return bytes(content)
    raise TypeError(
        f"`content` must be bytes / bytearray / memoryview, got {type(content).__name__}",
    )


def _build_upload_body(
    *,
    workspace_id: str,
    name: str,
    content: Union[bytes, bytearray, memoryview],
    mime_type: str,
    folder_id: Optional[str],
    folder_path: Optional[str],
) -> dict:
    raw = _ensure_bytes(content)
    if len(raw) == 0:
        raise ValueError("`content` must not be empty")
    body: dict = {
        "workspaceId": workspace_id,
        "name": name,
        "contentBase64": base64.b64encode(raw).decode("ascii"),
        "mimeType": mime_type,
    }
    if folder_id is not None:
        body["folderId"] = folder_id
    if folder_path is not None:
        body["folderPath"] = folder_path
    return body


def _decode_list_result(body: dict) -> DriveListResult:
    files = [
        DriveFile(
            id=f["id"],
            name=f["name"],
            mime_type=f["mimeType"],
            is_folder=bool(f.get("isFolder", False)),
        )
        for f in body.get("files", [])
    ]
    return DriveListResult(
        folder_id=body.get("folderId", ""),
        files=files,
        next_page_token=body.get("nextPageToken"),
    )


def _decode_download_result(
    response_bytes: bytes, headers: dict, *, file_id_hint: str,
) -> DriveDownloadResult:
    """Parse a Drive read response into the typed result.

    The server stamps file metadata into `X-Esoul-Drive-*` response
    headers (Drive details are mostly in the body for list/upload, but
    for downloads we want to stream bytes through unmodified).
    """
    file_id = headers.get("x-esoul-drive-file-id", file_id_hint)
    original_mime = headers.get("x-esoul-drive-original-mime-type", "")
    exported_flag = headers.get("x-esoul-drive-exported", "false").lower() == "true"
    mime_type = headers.get("content-type", "").split(";")[0].strip() or "application/octet-stream"
    # Filename comes from Content-Disposition: attachment; filename="..."
    cd = headers.get("content-disposition", "")
    file_name = ""
    if "filename=" in cd:
        try:
            from urllib.parse import unquote

            raw = cd.split("filename=", 1)[1].strip()
            # Strip surrounding quotes if present.
            if raw.startswith('"') and raw.endswith('"'):
                raw = raw[1:-1]
            file_name = unquote(raw)
        except (IndexError, ValueError):
            file_name = ""
    return DriveDownloadResult(
        content=response_bytes,
        file_id=file_id,
        mime_type=mime_type,
        original_mime_type=original_mime,
        exported=exported_flag,
        file_name=file_name,
    )


def _decode_upload_result(body: dict) -> DriveUploadResult:
    return DriveUploadResult(
        drive_file_id=body["driveFileId"],
        name=body.get("name", ""),
        parent_folder_id=body.get("parentFolderId", ""),
        web_view_link=body.get("webViewLink"),
    )


# ─── Sync resource ───────────────────────────────────────────────────────


class DriveResource:
    """Sync Drive operations. All methods raise typed exceptions on failure:
    `DriveNotConnected` when the workspace has no Drive OAuth, `NotFoundError`
    on missing folder/file, `DriveError` on Drive-side failures.
    """

    def __init__(self, transport: SyncTransport) -> None:
        self._transport = transport

    def list_folder(
        self,
        *,
        workspace_id: str,
        folder_id: Optional[str] = None,
        page_token: Optional[str] = None,
    ) -> DriveListResult:
        """List one folder's direct children. `folder_id=None` means root.

        Pagination: the server caps results at 25 entries per page. Call
        again with `page_token=result.next_page_token` to get the next
        page. For convenience, prefer `iter_folder` (forthcoming).
        """
        body: dict = {"workspaceId": workspace_id}
        if folder_id is not None:
            body["folderId"] = folder_id
        if page_token is not None:
            body["pageToken"] = page_token
        response = self._transport.request(
            "POST", "/api/v1/drive/list", json_body=body,
        )
        return _decode_list_result(response.json())

    def read_file(self, *, workspace_id: str, file_id: str) -> DriveDownloadResult:
        """Download bytes for a Drive file.

        Google-native docs (Docs/Sheets/Slides) export as PDF
        automatically — check `result.exported` to know.
        """
        response = self._transport.request(
            "POST",
            "/api/v1/drive/read",
            json_body={"workspaceId": workspace_id, "fileId": file_id},
        )
        return _decode_download_result(
            response.content, dict(response.headers), file_id_hint=file_id,
        )

    def upload_file(
        self,
        *,
        workspace_id: str,
        name: str,
        content: Union[bytes, bytearray, memoryview],
        mime_type: str,
        folder_id: Optional[str] = None,
        folder_path: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> DriveUploadResult:
        """Upload bytes to Drive.

        Folder selection precedence:
          - `folder_id` (opaque Drive id) wins if provided.
          - `folder_path` (e.g. "photos/2024") walks from root by folder
            name; all path segments must already exist as folders.
          - Neither → root.

        Drive lacks native idempotency — the SDK's Idempotency-Key
        prevents duplicate uploads ONLY within the server-side cache
        window (24 h). For long-lived idempotency, use deterministic
        file names + check before upload.
        """
        body = _build_upload_body(
            workspace_id=workspace_id,
            name=name,
            content=content,
            mime_type=mime_type,
            folder_id=folder_id,
            folder_path=folder_path,
        )
        response = self._transport.request(
            "POST",
            "/api/v1/drive/upload",
            json_body=body,
            idempotency_key=_resolve_idempotency_key(idempotency_key),
        )
        return _decode_upload_result(response.json())


# ─── Async resource ──────────────────────────────────────────────────────


class AsyncDriveResource:
    """Async mirror of `DriveResource`."""

    def __init__(self, transport: AsyncTransport) -> None:
        self._transport = transport

    async def list_folder(
        self,
        *,
        workspace_id: str,
        folder_id: Optional[str] = None,
        page_token: Optional[str] = None,
    ) -> DriveListResult:
        body: dict = {"workspaceId": workspace_id}
        if folder_id is not None:
            body["folderId"] = folder_id
        if page_token is not None:
            body["pageToken"] = page_token
        response = await self._transport.request(
            "POST", "/api/v1/drive/list", json_body=body,
        )
        return _decode_list_result(response.json())

    async def read_file(
        self, *, workspace_id: str, file_id: str,
    ) -> DriveDownloadResult:
        response = await self._transport.request(
            "POST",
            "/api/v1/drive/read",
            json_body={"workspaceId": workspace_id, "fileId": file_id},
        )
        return _decode_download_result(
            response.content, dict(response.headers), file_id_hint=file_id,
        )

    async def upload_file(
        self,
        *,
        workspace_id: str,
        name: str,
        content: Union[bytes, bytearray, memoryview],
        mime_type: str,
        folder_id: Optional[str] = None,
        folder_path: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> DriveUploadResult:
        body = _build_upload_body(
            workspace_id=workspace_id,
            name=name,
            content=content,
            mime_type=mime_type,
            folder_id=folder_id,
            folder_path=folder_path,
        )
        response = await self._transport.request(
            "POST",
            "/api/v1/drive/upload",
            json_body=body,
            idempotency_key=_resolve_idempotency_key(idempotency_key),
        )
        return _decode_upload_result(response.json())


__all__ = [
    "DriveFile",
    "DriveListResult",
    "DriveDownloadResult",
    "DriveUploadResult",
    "DriveResource",
    "AsyncDriveResource",
]

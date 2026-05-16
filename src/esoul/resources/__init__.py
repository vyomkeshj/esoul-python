"""Resource modules.

Resources are the typed surface the user code calls — `client.drive.list_folder(...)`,
`client.dispatch.event(...)`. Each resource wraps a transport instance and
translates between the SDK's typed dataclasses and the wire JSON.

This v1 ships hand-written resources for the low-level dispatch + drive
operations. Per-app typed resources (spreadsheet.rows, slideshow.slides,
etc.) land here as the codegen pipeline emits them.
"""

from .dispatch import (
    AsyncDispatchResource,
    BatchEvent,
    BatchResult,
    DescribeResult,
    DispatchResource,
    DispatchResult,
    EventInfo,
    NamespaceInfo,
    ReadStateResult,
    SessionInfo,
)
from .drive import (
    AsyncDriveResource,
    DriveDownloadResult,
    DriveFile,
    DriveListResult,
    DriveResource,
    DriveUploadResult,
)

__all__ = [
    "AsyncDispatchResource",
    "AsyncDriveResource",
    "BatchEvent",
    "BatchResult",
    "DescribeResult",
    "DispatchResource",
    "DispatchResult",
    "DriveDownloadResult",
    "DriveFile",
    "DriveListResult",
    "DriveResource",
    "DriveUploadResult",
    "EventInfo",
    "NamespaceInfo",
    "ReadStateResult",
    "SessionInfo",
]

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class MailboxCredentials:
    email: str
    password: str


@dataclass(frozen=True)
class MailFolderSummary:
    name: str
    delimiter: str | None = None
    flags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MailAttachmentSummary:
    id: str
    filename: str | None
    content_type: str
    size: int | None = None
    disposition: str | None = None
    is_inline: bool = False


@dataclass(frozen=True)
class MailAttachmentContent:
    summary: MailAttachmentSummary
    content: bytes


@dataclass(frozen=True)
class MailMessageSummary:
    uid: str
    folder: str
    subject: str
    sender: str
    to: tuple[str, ...] = field(default_factory=tuple)
    cc: tuple[str, ...] = field(default_factory=tuple)
    date: datetime | None = None
    message_id: str = ""
    flags: tuple[str, ...] = field(default_factory=tuple)
    size: int | None = None
    has_attachments: bool = False


@dataclass(frozen=True)
class MailMessageSummaryPage:
    messages: tuple[MailMessageSummary, ...] = field(default_factory=tuple)
    has_more: bool = False
    next_before_uid: str | None = None


@dataclass(frozen=True)
class MailMessageDetail:
    uid: str
    folder: str
    subject: str
    sender: str
    to: tuple[str, ...] = field(default_factory=tuple)
    cc: tuple[str, ...] = field(default_factory=tuple)
    date: datetime | None = None
    message_id: str = ""
    flags: tuple[str, ...] = field(default_factory=tuple)
    size: int | None = None
    text_body: str = ""
    html_body: str = ""
    attachments: tuple[MailAttachmentSummary, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MailMessageMoveFailure:
    uid: str
    error: str
    detail: str


@dataclass(frozen=True)
class MailMessageMoveToTrashResult:
    trash_folder: str
    moved_to_trash: tuple[str, ...] = field(default_factory=tuple)
    failed: tuple[MailMessageMoveFailure, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MailMessageRestoreResult:
    target_folder: str
    restored: tuple[str, ...] = field(default_factory=tuple)
    failed: tuple[MailMessageMoveFailure, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SendMailRequest:
    to: tuple[str, ...]
    subject: str
    text_body: str = ""
    html_body: str = ""
    cc: tuple[str, ...] = field(default_factory=tuple)
    bcc: tuple[str, ...] = field(default_factory=tuple)
    reply_to: str | None = None
    from_display_name: str = ""
    attachments: tuple["SendMailAttachment", ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SendMailAttachment:
    filename: str
    content_type: str
    content: bytes

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
    path: str = ""
    display_name: str = ""
    parent_path: str | None = None
    depth: int = 0
    selectable: bool = True

    def __post_init__(self):
        path = self.path or self.name
        display_name = self.display_name
        parent_path = self.parent_path
        depth = self.depth
        if not display_name:
            if self.delimiter:
                parts = path.split(self.delimiter)
                display_name = parts[-1] if parts else path
                if len(parts) > 1:
                    parent_path = self.delimiter.join(parts[:-1])
                    depth = len(parts) - 1
            else:
                display_name = path
        selectable = self.selectable and not any(flag.lower() == "noselect" for flag in self.flags)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "parent_path", parent_path)
        object.__setattr__(self, "depth", depth)
        object.__setattr__(self, "selectable", selectable)


@dataclass(frozen=True)
class MailAttachmentSummary:
    id: str
    filename: str | None
    content_type: str
    size: int | None = None
    disposition: str | None = None
    is_inline: bool = False
    content_id: str = ""
    is_visible: bool = True


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
    has_visible_attachments: bool | None = None
    in_reply_to: tuple[str, ...] = field(default_factory=tuple)
    references: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if self.has_visible_attachments is None:
            object.__setattr__(self, "has_visible_attachments", self.has_attachments)


@dataclass(frozen=True)
class MailMessageSummaryPage:
    messages: tuple[MailMessageSummary, ...] = field(default_factory=tuple)
    has_more: bool = False
    next_before_uid: str | None = None


@dataclass(frozen=True)
class MailConversationParticipant:
    name: str
    email: str


@dataclass(frozen=True)
class MailConversationSummary:
    conversation_id: str
    message_count: int
    reply_count: int
    has_unread: bool
    has_attachments: bool
    has_visible_attachments: bool
    participants: tuple[MailConversationParticipant, ...]
    root_message: MailMessageSummary
    replies: tuple[MailMessageSummary, ...] = field(default_factory=tuple)
    latest_date: datetime | None = None


@dataclass(frozen=True)
class MailConversationSummaryPage:
    conversations: tuple[MailConversationSummary, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MailUnifiedMessageSummary:
    summary: MailMessageSummary
    direction: str


@dataclass(frozen=True)
class MailUnifiedConversationSummary:
    conversation_id: str
    message_count: int
    reply_count: int
    has_unread: bool
    has_attachments: bool
    has_visible_attachments: bool
    participants: tuple[MailConversationParticipant, ...]
    messages: tuple[MailUnifiedMessageSummary, ...] = field(default_factory=tuple)
    latest_date: datetime | None = None


@dataclass(frozen=True)
class MailUnifiedConversationSummaryPage:
    folders: tuple[str, ...] = field(default_factory=tuple)
    conversations: tuple[MailUnifiedConversationSummary, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MailboxAccountSummary:
    unread_count: int = 0
    important_count: int = 0


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
    has_visible_attachments: bool | None = None

    def __post_init__(self):
        if self.has_visible_attachments is None:
            object.__setattr__(self, "has_visible_attachments", any(_attachment_is_visible(attachment) for attachment in self.attachments))


def _attachment_is_visible(attachment):
    return attachment.is_visible


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
class ForwardSourceMessage:
    folder: str
    uid: str
    attachment_ids: tuple[str, ...]


@dataclass(frozen=True)
class SendMailAttachment:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class SendMailRequest:
    to: tuple[str, ...]
    subject: str
    text_body: str = ""
    html_body: str = ""
    cc: tuple[str, ...] = field(default_factory=tuple)
    bcc: tuple[str, ...] = field(default_factory=tuple)
    reply_to: str | None = None
    in_reply_to: str = ""
    references: tuple[str, ...] = field(default_factory=tuple)
    from_display_name: str = ""
    attachments: tuple[SendMailAttachment, ...] = field(default_factory=tuple)
    forward_source_message: ForwardSourceMessage | None = None

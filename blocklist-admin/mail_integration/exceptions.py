class MailIntegrationError(Exception):
    """Base exception for normalized mail integration failures."""


class MailAuthError(MailIntegrationError):
    """Raised when IMAP or SMTP authentication fails."""


class MailConnectionError(MailIntegrationError):
    """Raised when a mail service cannot be reached."""


class MailTimeoutError(MailConnectionError):
    """Raised when a mail service operation times out."""


class MailProtocolError(MailIntegrationError):
    """Raised when a mail service returns an unexpected protocol response."""


class MailInvalidOperationError(MailIntegrationError):
    """Raised when a requested mailbox operation is intentionally unsupported."""


class MailAttachmentNotFoundError(MailIntegrationError):
    """Raised when a requested message attachment cannot be found."""


class MailSendError(MailIntegrationError):
    """Raised when SMTP accepts a connection but fails to send mail."""

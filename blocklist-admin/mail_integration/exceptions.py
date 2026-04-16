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


class MailSendError(MailIntegrationError):
    """Raised when SMTP accepts a connection but fails to send mail."""


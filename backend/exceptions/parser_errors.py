class ParserError(Exception):
    """Raised when document parsing fails."""


class UnsupportedFileTypeError(ParserError):
    """Raised when file extension is outside the configured parser boundary."""

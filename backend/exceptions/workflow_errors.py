class WorkflowError(Exception):
    """Raised when a session cannot move to the requested workflow stage."""


class SessionRevisionConflictError(WorkflowError):
    """Raised when a stale session snapshot would overwrite newer state."""


class SetupNotReadyError(WorkflowError):
    """Raised when rehearsal/guidance/report is requested before setup is complete."""


class MaxTurnsReachedError(WorkflowError):
    """Raised when the rehearsal has reached the configured max user turns."""

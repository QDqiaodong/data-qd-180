"""Distinguishable workbench failures."""


class WorkbenchError(Exception):
    """A business or storage failure with a stable machine-readable code."""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

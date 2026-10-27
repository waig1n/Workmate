"""Product execution errors shared by HTTP adapters and command services."""


class ApprovalConflict(RuntimeError):
    """The submitted decision cannot be applied to the current Task state."""


class ModelConfigError(Exception):
    """The configured model cannot be used for the requested Task."""

"""Exception types shown to users as plain error messages."""


class PyxbuildError(Exception):
    """An error with a message meant for the user; the CLI prints it without a traceback."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint

    def __str__(self) -> str:
        text = super().__str__()
        if self.hint:
            text += f"\n  hint: {self.hint}"
        return text


class ConfigError(PyxbuildError):
    """Invalid or incomplete project configuration."""


class ResolveError(PyxbuildError):
    """Dependencies cannot be resolved or locked for the target."""


class DownloadError(PyxbuildError):
    """A download failed or its hash did not match."""

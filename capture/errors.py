"""
Custom exceptions and friendly error handling for CapTure.

All exceptions derive from CapTureError. The friendly_error decorator
wraps any function to catch exceptions and display user-friendly messages
instead of raw tracebacks.
"""

from __future__ import annotations

import sys
import traceback
from functools import wraps
from typing import Callable, TypeVar


# ── Exception Hierarchy ──────────────────────────────────────────────


class CapTureError(Exception):
    """Base exception for all CapTure errors.

    Every CapTure-specific error extends this class so the UI layer
    can catch them uniformly.
    """

    def __init__(self, message: str, *args: object) -> None:
        super().__init__(message, *args)
        self.message = message

    def friendly(self) -> str:
        """Return a user-friendly error message (no traceback)."""
        return f"[CapTure] {self.message}"


class CaptureError(CapTureError):
    """Raised when screen capture fails (dxcam, display not found, etc.)."""


class AudioError(CapTureError):
    """Raised when audio capture fails (device busy, format unsupported, etc.)."""


class EncodeError(CapTureError):
    """Raised when video or audio encoding fails (codec, MSMF, COM issue)."""


class MuxerError(CapTureError):
    """Raised when muxing video+audio into MP4 fails (Media Foundation COM)."""


class ConfigError(CapTureError):
    """Raised when the configuration is invalid or inconsistent."""


class DependencyError(CapTureError):
    """Raised when a required system dependency is missing."""


# ── Friendly Error Decorator ─────────────────────────────────────────


F = TypeVar("F", bound=Callable[..., object])


def friendly_error(
    fallback_message: str = "An unexpected error occurred.",
    exit_on_error: bool = False,
) -> Callable[[F], F]:
    """Decorator that catches all exceptions and prints friendly messages.

    Args:
        fallback_message: Default message for unexpected (non-CapTure) errors.
        exit_on_error: If True, calls sys.exit(1) after displaying the error.

    Returns:
        A decorated function that never leaks raw tracebacks to stdout.
    """

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: object, **kwargs: object) -> object:
            try:
                return func(*args, **kwargs)
            except CapTureError as exc:
                print(f"\n  {exc.friendly()}", file=sys.stderr)
                if exit_on_error:
                    sys.exit(1)
                return None
            except Exception as exc:
                # Unexpected error: log traceback but show friendly message.
                traceback.print_exc(file=sys.stderr)
                print(
                    f"\n  [CapTure] {fallback_message}\n"
                    f"  Details: {exc}",
                    file=sys.stderr,
                )
                if exit_on_error:
                    sys.exit(1)
                return None

        return wrapper  # type: ignore[return-value]

    return decorator


def show_error(exc: Exception) -> None:
    """Display a friendly error message for any exception.

    Use this in non-decorator contexts (e.g., in event handlers).

    Args:
        exc: The exception to display.
    """
    if isinstance(exc, CapTureError):
        print(f"\n  {exc.friendly()}", file=sys.stderr)
    else:
        traceback.print_exc(file=sys.stderr)
        print(
            f"\n  [CapTure] An unexpected error occurred.\n"
            f"  Details: {exc}",
            file=sys.stderr,
        )

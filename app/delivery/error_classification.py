from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorKind(StrEnum):
    RETRY = "RETRY"
    PERMANENT = "PERMANENT"
    AMBIGUOUS = "AMBIGUOUS"
    CLEAN_ABSENT = "CLEAN_ABSENT"


@dataclass(frozen=True)
class ErrorDecision:
    kind: ErrorKind
    category: str
    retry_after_seconds: float | None = None


def classify_telegram_error(error: Exception, *, operation: str) -> ErrorDecision:
    """Conservative classifier: only clear pre-send failures are retried for sends."""
    name = type(error).__name__
    message = str(error).lower()
    retry_after = getattr(error, "retry_after", None)
    if retry_after is not None or "too many requests" in message:
        return ErrorDecision(ErrorKind.RETRY, "RATE_LIMIT", float(retry_after or 5))
    # A post may have been removed manually before this campaign reaches its
    # cleanup turn.  Telegram reports that as a Bad Request, but it is a
    # successful cleanup outcome from the campaign's point of view.  Do not
    # strand the rest of an album (or the whole campaign) on that one ID.
    if operation in {"delete", "edit"} and any(token in message for token in (
        "message to delete not found",
        "message to edit not found",
        "message not found",
        "message_id_invalid",
    )):
        return ErrorDecision(ErrorKind.CLEAN_ABSENT, "ALREADY_ABSENT")
    if operation == "delete" and "message can't be deleted" in message:
        # This is normally a Telegram deletion-policy/window restriction, not
        # evidence that a later channel message displaced the campaign post.
        # Keep the live-state pointer and surface the exact blocker instead of
        # falsely archiving the campaign as cleaned.
        return ErrorDecision(ErrorKind.PERMANENT, "DELETE_NOT_ALLOWED")
    if any(token in message for token in (
        "bot was kicked", "bot is not a member", "chat not found", "forbidden", "not enough rights",
        "have no rights", "channel private", "message can't be deleted",
    )):
        return ErrorDecision(ErrorKind.PERMANENT, "ACCESS_OR_PERMISSION")
    if operation == "send" and any(token in name.lower() + message for token in ("timeout", "network", "clientconnector")):
        # The server may already have accepted the request; never blindly duplicate it.
        return ErrorDecision(ErrorKind.AMBIGUOUS, "UNKNOWN_SEND_STATE")
    if any(token in message for token in ("bad request", "invalid", "unsupported")):
        return ErrorDecision(ErrorKind.PERMANENT, "INVALID_PAYLOAD")
    return ErrorDecision(ErrorKind.RETRY, "TRANSIENT", 5)

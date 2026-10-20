"""Pure GitHub event normalization and bounded IRC message rendering."""

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit


class InvalidEvent(ValueError):
    """A supported event has invalid fields (never includes payload values)."""


@dataclass(frozen=True)
class Announcement:
    text: str
    url: str


def _clean(value: str) -> str:
    return " ".join(
        "".join(
            c for c in value if unicodedata.category(c) not in {"Cc", "Cf", "Cs"}
        ).split()
    )


def _object(parent: dict[str, object], key: str) -> dict[str, object]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise InvalidEvent(key)
    return value


def _string(parent: dict[str, object], key: str, *, optional: bool = False) -> str:
    value = parent.get(key)
    if optional and value is None:
        return "unknown"
    if not isinstance(value, str):
        raise InvalidEvent(key)
    result = _clean(value)
    if not result:
        if optional:
            return "unknown"
        raise InvalidEvent(key)
    return result


def _number(parent: dict[str, object], key: str, default: int | None = None) -> int:
    value = parent.get(key, default)
    if type(value) is not int or value <= 0:
        raise InvalidEvent(key)
    return value


def _boolean(parent: dict[str, object], key: str) -> bool:
    value = parent.get(key)
    if type(value) is not bool:
        raise InvalidEvent(key)
    return value


def _url(parent: dict[str, object], key: str = "html_url") -> str:
    value = parent.get(key)
    if not isinstance(value, str) or any(
        c.isspace() or unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value
    ):
        raise InvalidEvent(key)
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme == "https"
            and parts.hostname == "github.com"
            and parts.username is None
            and parts.password is None
            and parts.port in {None, 443}
            and parts.netloc.lower() in {"github.com", "github.com:443"}
        )
    except ValueError:
        valid = False
    if not valid:
        raise InvalidEvent(key)
    return value


def _actor(payload: dict[str, object]) -> str:
    if payload.get("sender") is None:
        return "unknown"
    return _string(_object(payload, "sender"), "login", optional=True)


def normalize(event: str, payload: dict[str, object]) -> Announcement | None:
    """Normalize supported announcements; ignore unrelated events and actions."""
    if not isinstance(payload, dict):
        raise InvalidEvent("payload")
    action = payload.get("action")
    if event == "pull_request":
        if action not in ("opened", "reopened", "ready_for_review", "closed"):
            return None
        subject = _object(payload, "pull_request")
        number = _number(subject, "number")
        title = _string(subject, "title")
        label = "ready for review" if action == "ready_for_review" else action
        if action == "opened" and _boolean(subject, "draft"):
            label = "opened (draft)"
        if action == "closed" and _boolean(subject, "merged"):
            label = "merged"
        text = f"PR #{number} {label} by {_actor(payload)}: {title}"
        url = _url(subject)
    elif event == "issues":
        if action not in ("opened", "reopened", "closed"):
            return None
        subject = _object(payload, "issue")
        number = _number(subject, "number")
        title = _string(subject, "title")
        text = f"Issue #{number} {action} by {_actor(payload)}: {title}"
        url = _url(subject)
    elif event == "pull_request_review":
        if action != "submitted":
            return None
        review = _object(payload, "review")
        state = _string(review, "state")
        if state not in ("approved", "changes_requested"):
            return None
        reviewer = _string(_object(review, "user"), "login")
        subject = _object(payload, "pull_request")
        number = _number(subject, "number")
        label = "approved" if state == "approved" else "changes requested"
        text = f"PR #{number} {label} by {reviewer}"
        url = _url(subject)
    elif event == "workflow_run":
        if action != "completed":
            return None
        run = _object(payload, "workflow_run")
        _number(run, "id")
        attempt = _number(run, "run_attempt", 1)
        name = _string(run, "name")
        branch = _string(run, "head_branch", optional=True)
        sha = _string(run, "head_sha", optional=True)[:7]
        conclusion = _string(run, "conclusion", optional=True)
        text = f"CI {name} on {branch} @{sha}: {conclusion}"
        if attempt > 1:
            text += f" (attempt {attempt})"
        url = _url(run)
    else:
        return None
    repository = _string(_object(payload, "repository"), "full_name")
    return Announcement(f"[{repository}] {text}", url)


def format_message(announcement: Announcement, channel: str) -> str | None:
    """Return one message body fitting a 400-byte full outgoing IRC line."""
    if not re.fullmatch(r"[#&][\x21-\x7e]{0,79}", channel) or any(
        c in channel for c in ",:"
    ):
        return None
    try:
        url = _url({"html_url": announcement.url})
    except InvalidEvent:
        return None
    available = 400 - len(f"PRIVMSG {channel} :\r\n".encode())
    url_bytes = len(url.encode("utf-8"))
    if url_bytes > available:
        return None
    text = _clean(announcement.text)
    text_budget = available - url_bytes - 1
    if not text or text_budget <= 0:
        return url
    prefix = (
        text.encode("utf-8")[:text_budget].decode("utf-8", errors="ignore").rstrip()
    )
    return f"{prefix} {url}" if prefix else url

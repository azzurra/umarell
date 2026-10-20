from copy import deepcopy
from typing import Any

import pytest

from umarell.events import Announcement, InvalidEvent, format_message, normalize

URL = "https://github.com/owner/repo/pull/12"


def payload() -> dict[str, Any]:
    return {
        "repository": {"id": 1, "full_name": "owner/repo"},
        "sender": {"login": "actor"},
        "action": "opened",
        "pull_request": {
            "number": 12,
            "title": "A change",
            "html_url": URL,
            "draft": False,
            "merged": False,
        },
        "issue": {"number": 4, "title": "A bug", "html_url": URL},
        "review": {"state": "approved", "user": {"login": "reviewer"}},
        "workflow_run": {"id": 5, "name": "Tests", "html_url": URL},
    }


@pytest.mark.parametrize(
    "action,extra,label",
    [
        ("opened", {}, "opened"),
        ("opened", {"draft": True}, "opened (draft)"),
        ("reopened", {}, "reopened"),
        ("ready_for_review", {}, "ready for review"),
        ("closed", {}, "closed"),
        ("closed", {"merged": True}, "merged"),
    ],
)
def test_pr(action: str, extra: dict[str, object], label: str) -> None:
    data = payload()
    data["action"] = action
    data["pull_request"].update(extra)
    assert normalize("pull_request", data) == Announcement(
        f"[owner/repo] PR #12 {label} by actor: A change", URL
    )


@pytest.mark.parametrize("action", ["opened", "reopened", "closed"])
def test_issue(action: str) -> None:
    data = payload()
    data["action"] = action
    result = normalize("issues", data)
    assert result is not None
    assert result.text == f"[owner/repo] Issue #4 {action} by actor: A bug"


@pytest.mark.parametrize(
    "state,label",
    [("approved", "approved"), ("changes_requested", "changes requested")],
)
def test_review(state: str, label: str) -> None:
    data = payload()
    data["action"] = "submitted"
    data["review"]["state"] = state
    result = normalize("pull_request_review", data)
    assert result is not None
    assert result.text == f"[owner/repo] PR #12 {label} by reviewer"


@pytest.mark.parametrize(
    "conclusion",
    [
        None,
        "success",
        "failure",
        "cancelled",
        "skipped",
        "timed_out",
        "neutral",
        "action_required",
        "stale",
        "future_value",
    ],
)
def test_workflow_conclusions_and_reruns(conclusion: str | None) -> None:
    data = payload()
    data["action"] = "completed"
    data["workflow_run"].update(
        conclusion=conclusion, head_branch="main", head_sha="abcdef01234", run_attempt=2
    )
    expected = conclusion or "unknown"
    result = normalize("workflow_run", data)
    assert result is not None
    assert (
        result.text == f"[owner/repo] CI Tests on main @abcdef0: {expected} (attempt 2)"
    )


def test_workflow_defaults() -> None:
    data = payload()
    data["action"] = "completed"
    result = normalize("workflow_run", data)
    assert result is not None
    assert result.text == "[owner/repo] CI Tests on unknown @unknown: unknown"


@pytest.mark.parametrize(
    "event,action",
    [
        ("push", "opened"),
        ("pull_request", "edited"),
        ("issues", "deleted"),
        ("workflow_run", "requested"),
        ("pull_request_review", "dismissed"),
    ],
)
def test_ignored_event_needs_no_subject(event: str, action: str) -> None:
    assert normalize(event, {"action": action}) is None


def test_comment_review_ignored_without_pr() -> None:
    assert (
        normalize(
            "pull_request_review",
            {"action": "submitted", "review": {"state": "commented"}},
        )
        is None
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("number", True),
        ("number", 0),
        ("number", "12"),
        ("title", ""),
        ("title", None),
        ("draft", 1),
        ("html_url", "http://github.com/a"),
    ],
)
def test_pr_strict_fields(field: str, value: object) -> None:
    data = payload()
    data["pull_request"][field] = value
    with pytest.raises(InvalidEvent) as error:
        normalize("pull_request", data)
    assert str(error.value) == field


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com.evil/a",
        "https://user@github.com/a",
        "https://github.com:444/a",
        "https://github.com/a\r\nPRIVMSG",
        "https://github.com/a b",
        "https://github.com/a\u200b",
        "https://github.com:bad/a",
        "https://[github.com/a",
        "//github.com/a",
        "https://github.com:/a",
    ],
)
def test_invalid_urls(url: str) -> None:
    data = payload()
    data["pull_request"]["html_url"] = url
    with pytest.raises(InvalidEvent, match="^html_url$"):
        normalize("pull_request", data)


@pytest.mark.parametrize("value", [None, "", 12, "\x00\u200b"])
def test_repository_required(value: object) -> None:
    data = payload()
    data["repository"]["full_name"] = value
    with pytest.raises(InvalidEvent):
        normalize("pull_request", data)


def test_optional_actor_and_wrong_types() -> None:
    data = payload()
    del data["sender"]
    result = normalize("pull_request", data)
    assert result is not None
    assert "by unknown:" in result.text
    data["sender"] = {"login": 1}
    with pytest.raises(InvalidEvent):
        normalize("pull_request", data)
    data["action"] = "completed"
    for key in ["head_branch", "head_sha", "conclusion"]:
        broken = deepcopy(data)
        broken["workflow_run"][key] = False
        with pytest.raises(InvalidEvent):
            normalize("workflow_run", broken)


def test_clean_display_and_unicode() -> None:
    data = payload()
    data["pull_request"]["title"] = "  Caffè\x03\x00\r\n\u200b\ud800  修正  "
    result = normalize("pull_request", data)
    assert result is not None
    assert result.text.endswith("Caffè 修正")
    message = format_message(result, "#team")
    assert message is not None
    assert message.endswith("Caffè 修正 " + URL)


@pytest.mark.parametrize("channel", ["#x", "#" + "a" * 79])
def test_full_line_utf8_budget(channel: str) -> None:
    result = format_message(Announcement("🍕" * 1000, URL), channel)
    assert result is not None
    assert result.endswith(" " + URL)
    assert len(f"PRIVMSG {channel} :{result}\r\n".encode()) <= 400
    assert "�" not in result


def test_url_alone_and_oversize() -> None:
    channel = "#team"
    budget = 400 - len(f"PRIVMSG {channel} :\r\n".encode())
    url = "https://github.com/" + "a" * (budget - len("https://github.com/"))
    assert format_message(Announcement("hello", url), channel) == url
    assert format_message(Announcement("hello", url + "a"), channel) is None


@pytest.mark.parametrize(
    "channel", ["#x\r\nQUIT", "#a b", "#a,b", "#a:b", "room", "#é"]
)
def test_invalid_channel(channel: str) -> None:
    assert format_message(Announcement("hello", URL), channel) is None

"""Pure normalization of multica CLI JSON into database row dicts.

Contracts verified against the live CLI on 2026-07-15 (see tests/fixtures).
Missing required keys raise NormalizationError — a contract change must
surface in health, not silently produce empty rows.
"""

import re
from typing import Any, Dict, List, Optional

SP_LABEL_RE = re.compile(r"^SP:(\d+(?:\.\d+)?)$")


class NormalizationError(ValueError):
    """CLI output did not match the expected contract."""


def _require(obj: Dict[str, Any], key: str, context: str) -> Any:
    if key not in obj:
        raise NormalizationError(f"{context}: missing required key '{key}'")
    return obj[key]


def _int_field(obj: Dict[str, Any], key: str, context: str) -> int:
    value = _require(obj, key, context)
    if value is None:
        return 0
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise NormalizationError(f"{context}: key '{key}' is not a number: {value!r}")
    return int(value)


def normalize_runtime(obj: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": _require(obj, "id", "runtime"),
        "name": obj.get("name"),
        "provider": obj.get("provider"),
        "status": obj.get("status"),
        "device_info": obj.get("device_info"),
        "last_seen_at": obj.get("last_seen_at"),
        "created_at": obj.get("created_at"),
        "updated_at": obj.get("updated_at"),
    }


def normalize_agent(obj: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": _require(obj, "id", "agent"),
        "name": obj.get("name"),
        "model": obj.get("model"),
        "runtime_id": obj.get("runtime_id"),
        "description": obj.get("description"),
        "archived_at": obj.get("archived_at"),
        "created_at": obj.get("created_at"),
        "updated_at": obj.get("updated_at"),
    }


def normalize_project(obj: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": _require(obj, "id", "project"),
        "title": obj.get("title"),
        "description": obj.get("description"),
        "status": obj.get("status"),
        "priority": obj.get("priority"),
        "issue_count": obj.get("issue_count"),
        "done_count": obj.get("done_count"),
        "created_at": obj.get("created_at"),
        "updated_at": obj.get("updated_at"),
    }


def extract_story_points(obj: Dict[str, Any]) -> Optional[float]:
    """story_points from issue metadata, falling back to an `SP:N` label."""
    metadata = obj.get("metadata") or {}
    value = metadata.get("story_points")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    for label in obj.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else None
        match = SP_LABEL_RE.match(name or "")
        if match:
            return float(match.group(1))
    return None


def normalize_issue(obj: Dict[str, Any]) -> Dict[str, Any]:
    metadata = obj.get("metadata") or {}
    return {
        "id": _require(obj, "id", "issue"),
        "identifier": obj.get("identifier"),
        "number": obj.get("number"),
        "title": obj.get("title"),
        "status": obj.get("status"),
        "priority": obj.get("priority"),
        "project_id": obj.get("project_id"),
        "parent_issue_id": obj.get("parent_issue_id"),
        "stage": obj.get("stage"),
        "assignee_id": obj.get("assignee_id"),
        "assignee_type": obj.get("assignee_type"),
        "story_points": extract_story_points(obj),
        "estimation_model": metadata.get("estimation_model"),
        "created_at": obj.get("created_at"),
        "updated_at": _require(obj, "updated_at", "issue"),
    }


def normalize_daily_usage(obj: Dict[str, Any]) -> Dict[str, Any]:
    context = "daily_usage"
    return {
        "runtime_id": _require(obj, "runtime_id", context),
        "model": _require(obj, "model", context),
        "date": _require(obj, "date", context),
        "provider": obj.get("provider"),
        "input_tokens": _int_field(obj, "input_tokens", context),
        "output_tokens": _int_field(obj, "output_tokens", context),
        "cache_read_tokens": _int_field(obj, "cache_read_tokens", context),
        "cache_write_tokens": _int_field(obj, "cache_write_tokens", context),
    }


def normalize_issue_usage(issue_id: str, obj: Dict[str, Any]) -> Dict[str, Any]:
    context = f"issue_usage[{issue_id}]"
    return {
        "issue_id": issue_id,
        "task_count": _int_field(obj, "task_count", context),
        "total_input_tokens": _int_field(obj, "total_input_tokens", context),
        "total_output_tokens": _int_field(obj, "total_output_tokens", context),
        "total_cache_read_tokens": _int_field(obj, "total_cache_read_tokens", context),
        "total_cache_write_tokens": _int_field(obj, "total_cache_write_tokens", context),
    }


def normalize_run(obj: Dict[str, Any]) -> Dict[str, Any]:
    """A Multica task row, shared by `issue runs` and `agent tasks`."""
    return {
        "id": _require(obj, "id", "run"),
        "issue_id": obj.get("issue_id"),
        "agent_id": obj.get("agent_id"),
        "runtime_id": obj.get("runtime_id"),
        "kind": obj.get("kind"),
        "status": obj.get("status"),
        "attempt": obj.get("attempt"),
        "error": obj.get("error"),
        "created_at": obj.get("created_at"),
        "dispatched_at": obj.get("dispatched_at"),
        "started_at": obj.get("started_at"),
        "completed_at": obj.get("completed_at"),
    }


def normalize_activity(runtime_id: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    context = f"runtime_activity[{runtime_id}]"
    normalized = []
    for row in rows:
        normalized.append(
            {
                "runtime_id": runtime_id,
                "hour": _int_field(row, "hour", context),
                "count": _int_field(row, "count", context),
            }
        )
    return normalized

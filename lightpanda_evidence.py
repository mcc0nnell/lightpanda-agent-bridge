"""Privacy-conscious workflow recording, replay, and evidence receipts.

Receipts contain action templates, types and hashes of observations, never raw
page content or interpolated parameter values. They are integrity checks, not
cryptographic signatures or independent proof of website truth.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
from typing import Any

from lightpanda_service import BrowserSession, MAX_ACTIONS, checked_url

TOKEN = re.compile(r"^\{\{([a-zA-Z_][a-zA-Z0-9_]{0,63})\}\}$")
RUN_ID = re.compile(r"^[0-9a-f]{32}$")
RESERVED = {"start_url"}


def digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _store_dir(root: Path | None = None) -> Path:
    directory = Path(root or os.getenv(
        "LIGHTPANDA_RUN_DIR", str(Path.home() / ".local/state/lightpanda-agent-bridge/runs")
    )).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError("Evidence directory must not be a symlink")
    os.chmod(directory, 0o700)
    return directory


def _save(data: dict, root: Path | None = None) -> dict:
    directory = _store_dir(root)
    path = directory / (data["receipt_id"] + ".json")
    body = {**data, "receipt_sha256": digest(data)}
    # Exclusive creation; prevent overwriting receipts.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return body


def _load(receipt_id: str, root: Path | None = None) -> dict:
    if not isinstance(receipt_id, str) or not RUN_ID.fullmatch(receipt_id):
        raise ValueError("Invalid receipt id")
    path = _store_dir(root) / (receipt_id + ".json")
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("Evidence receipt not found")
    if path.stat().st_size > 256_000:
        raise ValueError("Evidence receipt exceeds size limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    checksum = data.pop("receipt_sha256", None)
    if digest(data) != checksum:
        raise ValueError("Evidence receipt integrity check failed")
    data["receipt_sha256"] = checksum
    return data


def _url_can_record(url: str) -> bool:
    # Query/fragment often carry tokens. URLs with either are runtime-only.
    from urllib.parse import urlsplit
    p = urlsplit(url)
    return bool(p.scheme in ("http", "https") and p.hostname and
                not p.username and not p.password and not p.query and not p.fragment)


def _placeholder(value: str) -> str | None:
    m = TOKEN.fullmatch(value) if isinstance(value, str) else None
    return m.group(1) if m else None


def _resolve(value: str, inputs: dict) -> str:
    key = _placeholder(value)
    if key is None:
        return value
    if key not in inputs or not isinstance(inputs[key], str):
        raise ValueError("A required replay input was not provided")
    if len(inputs[key]) > 50_000:
        raise ValueError("Replay input exceeds length limit")
    return inputs[key]


def _safe_plan(url: str, actions: list[dict]) -> tuple[dict, list[str]]:
    if not isinstance(actions, list) or not (1 <= len(actions) <= MAX_ACTIONS):
        raise ValueError("Supply 1 to MAX_ACTIONS actions")
    checked_url(url)
    required: set[str] = set()
    safe_url = url
    if not _url_can_record(url):
        safe_url = "{{start_url}}"
        required.add("start_url")
    safe_actions: list[dict] = []
    for i, action in enumerate(actions, 1):
        if not isinstance(action, dict) or "type" not in action:
            raise ValueError("Action must have a type")
        typ = action["type"]
        if typ not in ("navigate", "fill", "click", "extract", "text", "attribute", "evaluate"):
            raise ValueError("Unsupported action type")
        cleaned = {k: action[k] for k in ("type", "selector", "name") if k in action}
        key = "value" if typ == "fill" else "expression" if typ == "evaluate" else "url" if typ == "navigate" else None
        if key:
            raw = action.get(key)
            if not isinstance(raw, str):
                raise ValueError("Missing or invalid action parameter")
            token = _placeholder(raw)
            if token:
                cleaned[key] = raw
                required.add(token)
            elif typ in ("fill", "evaluate") or (typ == "navigate" and not _url_can_record(raw)):
                placeholder = "redacted_step_%d" % i
                cleaned[key] = "{{" + placeholder + "}}"
                required.add(placeholder)
            else:
                cleaned[key] = raw
        safe_actions.append(cleaned)
    return {"url": safe_url, "actions": safe_actions}, sorted(required)


def _runtime_actions(actions: list[dict], inputs: dict) -> list[dict]:
    rendered = []
    for action in actions:
        a = dict(action)
        for k in ("value", "url", "expression"):
            if k in a:
                a[k] = _resolve(a[k], inputs)
        if a["type"] == "navigate":
            checked_url(a["url"])
        rendered.append(a)
    return rendered


def _new(kind: str, parent_id: str | None = None) -> dict:
    data = {
        "schema": "windanvil.browser-receipt.v1",
        "receipt_id": uuid.uuid4().hex,
        "kind": kind,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "outcome": "BLOCKED",
        "engine": "lightpanda",
        "steps": [],
    }
    if parent_id:
        data["parent_receipt_id"] = parent_id
    return data


def _observe(browser: Any, actions: list[dict], expected: list | None = None) -> tuple[list, str, str | None]:
    steps = []
    for i, action in enumerate(actions):
        start = time.monotonic()
        try:
            observed = browser.action(action)
            observed_digest = digest(observed.get("result"))
            reference = expected[i]["result_sha256"] if expected is not None else None
            state = "FAIL" if reference is not None and reference != observed_digest else "PASS"
            step = {
                "index": i + 1,
                "type": action["type"],
                "outcome": state,
                "result_sha256": observed_digest,
                "duration_ms": int((time.monotonic() - start) * 1000),
            }
            if reference is not None:
                step["matches_original"] = state == "PASS"
            steps.append(step)
            if state == "FAIL":
                return steps, "FAIL", "observation_mismatch"
        except Exception as exc:
            steps.append({"index": i + 1, "type": action["type"],
                          "outcome": "FAIL", "error_type": type(exc).__name__,
                          "duration_ms": int((time.monotonic() - start) * 1000)})
            return steps, "FAIL", "action_error"
    return steps, "PASS", None


def record(url: str, actions: list[dict], inputs: dict | None = None,
           *, root: Path | None = None, factory=BrowserSession) -> dict:
    """Record one workflow. Caller inputs are intentionally never written."""
    inputs = inputs or {}
    plan, required = _safe_plan(url, actions)
    receipt = _new("record")
    receipt.update({"workflow": plan, "workflow_sha256": digest(plan),
                    "required_replay_inputs": required})
    browser = None
    try:
        # Runtime can contain raw values; the safe plan cannot.
        runtime = _runtime_actions(actions, inputs)
        browser = factory(url)
        receipt["engine_version"] = browser.version.get("product")
        receipt["engine_sha256"] = browser.binary_sha256
        receipt["steps"], receipt["outcome"], error = _observe(browser, runtime)
        if error:
            receipt["reason"] = error
    except Exception as exc:
        receipt["outcome"] = "BLOCKED"
        receipt["reason"] = "execution_unavailable"
        receipt["error_type"] = type(exc).__name__
    finally:
        if browser:
            browser.close()
    return _save(receipt, root)


def replay(receipt_id: str, inputs: dict | None = None,
           *, root: Path | None = None, factory=BrowserSession) -> dict:
    """Replay a stored, integrity-checked workflow; compare observation hashes."""
    original = _load(receipt_id, root)
    if original.get("kind") != "record" or original.get("schema") != "windanvil.browser-receipt.v1":
        raise ValueError("Replay requires a recorded workflow")
    plan = original.get("workflow")
    if digest(plan) != original.get("workflow_sha256"):
        raise ValueError("Workflow integrity check failed")
    result = _new("replay", parent_id=receipt_id)
    result["source_outcome"] = original.get("outcome")
    result["workflow_sha256"] = original["workflow_sha256"]
    if original["outcome"] != "PASS":
        result["reason"] = "source_not_complete"
        return _save(result, root)
    supplied = inputs or {}
    if any(key not in supplied for key in original["required_replay_inputs"]):
        result["reason"] = "missing_replay_inputs"
        result["required_replay_inputs"] = original["required_replay_inputs"]
        return _save(result, root)
    browser = None
    try:
        url = _resolve(plan["url"], supplied)
        checked_url(url)
        actions = _runtime_actions(plan["actions"], supplied)
        browser = factory(url)
        result["engine_version"] = browser.version.get("product")
        result["engine_sha256"] = browser.binary_sha256
        expected = original["steps"]
        result["steps"], result["outcome"], reason = _observe(browser, actions, expected)
        if reason:
            result["reason"] = reason
    except Exception as exc:
        result["outcome"] = "BLOCKED"
        result["reason"] = "execution_unavailable"
        result["error_type"] = type(exc).__name__
    finally:
        if browser:
            browser.close()
    return _save(result, root)


def summary(receipt: dict) -> dict:
    """Return safe metadata without a local filesystem path."""
    return {k: receipt[k] for k in (
        "receipt_id", "parent_receipt_id", "kind", "outcome", "reason", "created_at",
        "workflow_sha256", "receipt_sha256", "required_replay_inputs",
        "steps", "engine_version", "engine_sha256",
    ) if k in receipt}

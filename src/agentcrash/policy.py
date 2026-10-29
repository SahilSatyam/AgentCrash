"""Policy engine: load/validate ``policy/v1``, apply ``policy_patch/v1``.

A base policy is a YAML mapping validated against ``schemas/policy.json``.
A diagnoser patch is a JSON document validated against
``schemas/policy-patch.json``; its ``patch`` member is an RFC 7396 JSON
Merge Patch applied onto the base policy (objects merge key-wise, arrays
and scalars replace, ``null`` deletes). Rule fragments may use the
ADR-004 shorthand keys (``max_retries``, ``retry_with_idempotency_key``,
``check_transaction_status``/``confirm_with_user``/``abort`` actions),
which are normalized to the canonical ``policy/v1`` shape before the
result is schema-validated. Anything outside the schema raises
:exc:`PolicyRejectedError` (``E_POLICY_REJECTED``, exit 4) — never a
silent ignore. No wall-clock, no network, no model calls here; the single
opt-in live-model fetch lives in :func:`fetch_live_patch` and only runs
when ``LLAMA_BASE_URL`` is set.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

import jsonschema
import yaml

SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "schemas"

DEFAULT_MODEL = "llama-3.3-70b"

ACTION_ALIASES = {
    "check_transaction_status": "check_then_act",
    "confirm_with_user": "require_confirmation",
    "abort": "fail",
}


class PolicyError(ValueError):
    """Base policy file is missing or invalid (E_CONFIG/E_SCHEMA, exit 2)."""


class PolicyRejectedError(ValueError):
    """Patch is not a valid policy_patch/v1 or violates policy/v1 (exit 4)."""


def _schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def load_policy(path: str | Path) -> tuple[dict[str, Any], str]:
    """Parse and schema-validate a ``policy/v1`` YAML file.

    Returns ``(data, raw_text)``; problems raise :exc:`PolicyError`.
    """
    raw = Path(path)
    try:
        text = raw.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"cannot read policy {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"malformed YAML in {path}: {exc}") from exc
    validate_policy_data(data, source=str(path))
    return data, text


def validate_policy_data(data: Any, source: str = "policy") -> dict[str, Any]:
    """Validate parsed policy data; return it or raise :exc:`PolicyError`."""
    if not isinstance(data, dict):
        raise PolicyError(f"{source} must contain a YAML mapping")
    if data.get("schema_version") != "policy/v1":
        raise PolicyError(
            f"{source} schema_version must be 'policy/v1' " f"(got {data.get('schema_version')!r})"
        )
    try:
        jsonschema.validate(data, _schema("policy.json"))
    except jsonschema.ValidationError as exc:
        raise PolicyError(f"{source} violates policy/v1 ({exc.message})") from exc
    return data


def load_patch_doc(path: str | Path) -> dict[str, Any]:
    """Parse and envelope-validate a ``policy_patch/v1`` JSON file.

    Problems raise :exc:`PolicyRejectedError` (E_POLICY_REJECTED, exit 4).
    """
    raw = Path(path)
    try:
        text = raw.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyRejectedError(f"cannot read patch {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PolicyRejectedError(f"patch {path} is not valid JSON ({exc})") from exc
    return validate_patch_data(data, source=str(path))


def validate_patch_data(data: Any, source: str = "patch") -> dict[str, Any]:
    """Validate parsed patch data; return it or raise PolicyRejectedError."""
    if not isinstance(data, dict):
        raise PolicyRejectedError(f"{source} must contain a JSON object")
    if data.get("schema_version") != "policy_patch/v1":
        raise PolicyRejectedError(
            f"{source} schema_version must be 'policy_patch/v1' "
            f"(got {data.get('schema_version')!r})"
        )
    try:
        jsonschema.validate(data, _schema("policy-patch.json"))
    except jsonschema.ValidationError as exc:
        raise PolicyRejectedError(f"{source} violates policy_patch/v1 ({exc.message})") from exc
    if not isinstance(data.get("patch"), dict):
        raise PolicyRejectedError(f"{source} 'patch' must be a JSON Merge Patch object")
    return data


def normalize_rule(rule: Any) -> dict[str, Any]:
    """Translate ADR-004 shorthand keys to canonical ``policy/v1`` shape."""
    if not isinstance(rule, dict):
        raise PolicyRejectedError(f"patch rule must be an object, got {rule!r}")
    out = dict(rule)
    retry = dict(out.get("retry") or {})
    if "max_retries" in out:
        value = out.pop("max_retries")
        if type(value) is not int or not 0 <= value <= 3:
            raise PolicyRejectedError(f"max_retries must be an int 0-3 (got {value!r})")
        retry.setdefault("max_attempts", value)
    if "retry_with_idempotency_key" in out:
        value = out.pop("retry_with_idempotency_key")
        if not isinstance(value, bool):
            raise PolicyRejectedError(f"retry_with_idempotency_key must be a bool (got {value!r})")
        retry.setdefault("require_idempotency_key", value)
    if retry:
        out["retry"] = retry
    action = out.get("action")
    if isinstance(action, str) and action in ACTION_ALIASES:
        out["action"] = ACTION_ALIASES[action]
    return out


def merge_patch(base: Any, patch: Any) -> Any:
    """Apply an RFC 7396 JSON Merge Patch (arrays replace, null deletes)."""
    if isinstance(patch, dict):
        out = dict(base) if isinstance(base, dict) else {}
        for key, value in patch.items():
            if value is None:
                out.pop(key, None)
            elif key in out:
                out[key] = merge_patch(out[key], value)
            else:
                out[key] = value
        return out
    return patch


def apply_patch(
    base_policy: dict[str, Any], patch_doc: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Apply a validated patch doc onto a base policy.

    Returns ``(new_policy, new_text)`` where ``new_text`` is stable
    canonical YAML usable for ``run_id`` derivation. Schema violations in
    the merged result raise :exc:`PolicyRejectedError`.
    """
    validate_patch_data(patch_doc)
    merged = merge_patch(base_policy, patch_doc["patch"])
    if not isinstance(merged, dict):
        raise PolicyRejectedError("patch must merge onto a policy object")
    rules = merged.get("rules")
    if isinstance(rules, list):
        merged["rules"] = [normalize_rule(rule) for rule in rules]
    try:
        jsonschema.validate(merged, _schema("policy.json"))
    except jsonschema.ValidationError as exc:
        raise PolicyRejectedError(f"patched policy violates policy/v1 ({exc.message})") from exc
    return merged, yaml.safe_dump(merged, sort_keys=True)


def _rule_key(rule: dict[str, Any]) -> str:
    return f"{rule.get('rule', '?')}/{rule.get('tool', '?')}"


def _flat(rule: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key in sorted(rule):
        value = rule[key]
        label = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flat(value, prefix=f"{label}."))
        else:
            flat[label] = json.dumps(value, sort_keys=True, default=str)
    return flat


def diff_policies(old: dict[str, Any], new: dict[str, Any]) -> str:
    """Render a one-screen human review of rule changes between policies."""
    before = {_rule_key(r): r for r in old.get("rules", []) if isinstance(r, dict)}
    after = {_rule_key(r): r for r in new.get("rules", []) if isinstance(r, dict)}
    lines = [f"policy diff ({len(after)} rule(s) after patch):"]
    for key in sorted(set(before) | set(after)):
        if key not in before:
            lines.append(f"+ {key}: {json.dumps(after[key], sort_keys=True, default=str)}")
        elif key not in after:
            lines.append(f"- {key} (removed)")
            continue
        else:
            old_flat, new_flat = _flat(before[key]), _flat(after[key])
            changed = False
            for field in sorted(set(old_flat) | set(new_flat)):
                if old_flat.get(field) != new_flat.get(field):
                    lines.append(
                        f"~ {key}: {field} "
                        f"{old_flat.get(field, '(absent)')} -> {new_flat.get(field, '(absent)')}"
                    )
                    changed = True
            if not changed:
                lines.append(f"= {key} (unchanged)")
    return "\n".join(lines)


def reference_patch(source_run_id: str) -> dict[str, Any]:
    """Return the fixture-backed v0 fix for the double-charge trace.

    Reconcile-before-retry: on an ambiguous ``charge_card`` timeout, read
    ``get_transaction_status`` first, then retry at most once reusing the
    same idempotency key so the ledger dedups the commit.
    """
    return {
        "schema_version": "policy_patch/v1",
        "source_run_id": source_run_id,
        "rationale": (
            "Blind retry_same on an ambiguous charge_card timeout committed "
            "twice. Reconcile first via get_transaction_status, then retry "
            "at most once with the same idempotency key."
        ),
        "patch": {
            "rules": [
                {
                    "rule": "on_tool_timeout",
                    "tool": "charge_card",
                    "action": "check_then_act",
                    "check": {"tool": "get_transaction_status", "by": "idempotency_key"},
                    "retry_with_idempotency_key": True,
                    "max_retries": 1,
                }
            ]
        },
    }


def fetch_live_patch(
    *,
    trace_summary: str,
    verdict_summary: str,
    base_policy: dict[str, Any],
    model: str,
    timeout_s: int = 30,
) -> dict[str, Any] | None:
    """Ask an OpenAI-compatible model for a patch; None means fall back.

    Only runs when ``LLAMA_BASE_URL`` is set (opt-in live path, F.5).
    Transport failures return None so the caller falls back to the
    fixture-backed reference patch. A 200 response that is not a valid
    ``policy_patch/v1`` raises :exc:`PolicyRejectedError`.
    """
    base_url = os.environ.get("LLAMA_BASE_URL", "").strip()
    if not base_url:
        return None
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Propose a recovery-policy fix as a single JSON "
                        "policy_patch/v1 document with one rule. Reply with "
                        "JSON only, no prose."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Failing trace: {trace_summary}\n"
                        f"Verdict: {verdict_summary}\n"
                        f"Base policy: {json.dumps(base_policy, sort_keys=True)}"
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("LLAMA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    try:
        content = payload["choices"][0]["message"]["content"]
        data = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise PolicyRejectedError(f"live model returned unparsable output ({exc})") from exc
    return validate_patch_data(data, source=f"live model {model!r}")

"""Fetch Claude subscription quota usage for terminal signals.

Claude Code transcripts carry no quota numbers, but the OAuth credentials
in ~/.claude/.credentials.json grant access to Anthropic's usage endpoint,
which reports used-percent for the rolling 5-hour and 7-day windows — the
same numbers `/usage` shows inside Claude Code. They are attached in the
shape codex rate limits already use (primary/secondary), so emitters can
format both providers identically. Codex needs none of this: its rollouts
carry the percentages and the hook forwards them directly.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from sandboxpulse.models import Signal, is_terminal

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_OAUTH_BETA = "oauth-2025-04-20"
_HTTP_TIMEOUT_S = 5.0
_CACHE_TTL_S = 60.0
# Bursts of terminal signals (several sessions stopping together) must not
# hammer the endpoint; one fetch a minute is plenty. Failures cache too.
_cache: dict[str, tuple[float, dict[str, float | str]]] = {}

# (endpoint payload key, signal field prefix, window length in minutes)
_WINDOWS = (
    ("five_hour", "primary", 300),
    ("seven_day", "secondary", 10080),
)


def _default_credentials_path() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def _read_token(path: Path) -> str | None:
    try:
        creds = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    return token if isinstance(token, str) and token else None


def _fetch_payload(token: str) -> dict:
    request = urllib.request.Request(
        _USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": _OAUTH_BETA,
        },
    )
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
        payload = json.loads(response.read())
    return payload if isinstance(payload, dict) else {}


def claude_limit_percents(credentials_path: Path | None = None) -> dict[str, float | str]:
    """Used-percent of the 5h/7d quota windows, shaped like codex rate limits.

    Returns e.g. {"primary_used_percent": 4.0, "primary_window_minutes": 300,
    "primary_resets_at": "2026-06-10T19:00:00+00:00", "secondary_..."}; an
    empty dict when credentials are missing or the endpoint fails, so the
    caller (and the message footer) can simply drop what is unavailable.
    """
    path = credentials_path or _default_credentials_path()
    cache_key = str(path)
    cached = _cache.get(cache_key)
    if cached and time.monotonic() < cached[0]:
        return cached[1]

    percents: dict[str, float | str] = {}
    token = _read_token(path)
    if token:
        try:
            payload = _fetch_payload(token)
        except Exception:
            payload = {}
        for payload_key, prefix, minutes in _WINDOWS:
            window = payload.get(payload_key)
            if not isinstance(window, dict):
                continue
            used = window.get("utilization")
            if isinstance(used, (int, float)):
                percents[f"{prefix}_used_percent"] = float(used)
                percents[f"{prefix}_window_minutes"] = minutes
                resets = window.get("resets_at")
                if isinstance(resets, str) and resets:
                    percents[f"{prefix}_resets_at"] = resets

    _cache[cache_key] = (time.monotonic() + _CACHE_TTL_S, percents)
    return percents


def enrich_signal_usage(signal: Signal, *, credentials_path: Path | None = None) -> None:
    """Attach quota used-percents to a terminal claude signal, in place.

    Codex signals already carry rate-limit percentages from the hook; only
    claude needs them fetched here. When nothing is available the signal is
    left untouched.
    """
    if not is_terminal(signal.state):
        return
    provider = str(signal.metadata.get("provider") or signal.agent_id.rsplit("-", 1)[0])
    if provider != "claude":
        return
    percents = claude_limit_percents(credentials_path)
    if not percents:
        return
    bucket = signal.metadata.get("usage")
    if not isinstance(bucket, dict):
        bucket = {}
        signal.metadata["usage"] = bucket
    bucket.update(percents)

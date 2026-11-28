"""Provider-scoped removals, distinct from the display-only model blocklist."""

from __future__ import annotations

from .naming import normalize_model_key

# 2026-11-28: chat completed but read/create/update/delete all failed tool tests.
# Do not retire models that were merely inconclusive (timeout or upstream error).
_MIORA_CHAT_ONLY = frozenset({"gpt-6-sol"})
MIORA_RETIRED_MESSAGE = "Miora model retired: file tool calls failed validation"


def model_retired(provider: str, model: str) -> bool:
    return provider == "miora" and normalize_model_key(model) in _MIORA_CHAT_ONLY

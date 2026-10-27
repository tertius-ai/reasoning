"""Explicit, model-aware reasoning effort validation and adapter settings.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from .store import StoreError

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
CLI_EFFORTS = (*EFFORTS[2:], "ultra")


def checked(value):
    if value is not None and value not in (*EFFORTS, *CLI_EFFORTS):
        raise StoreError(400, "select an advertised reasoning effort or provider default")
    return value


def openrouter_efforts(model):
    reasoning = model.get("reasoning")
    if not isinstance(reasoning, dict) or "supported_efforts" not in reasoning:
        return []
    levels = reasoning["supported_efforts"]
    if levels is None:
        levels = EFFORTS
    if not isinstance(levels, (list, tuple)):
        return []
    return [level for level in EFFORTS if level in levels and not (level == "none" and reasoning.get("mandatory"))]

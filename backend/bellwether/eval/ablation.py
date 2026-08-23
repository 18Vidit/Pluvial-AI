"""Ablations (design spec §8): does the enrichment earn its place?

Two ablations, both implemented as guidance-instruction overrides rather
than code branches — the cascade itself never changes, only what the
agents are told they may use, which keeps the ablation honest (same tools
available, same model, same prompts minus one instruction block).
"""
from __future__ import annotations

NO_MOISTURE_OVERRIDE = """
ABLATION MODE: ignore any moisture_history / trigger_state information
entirely, even if the tool returns it. Reason only from static soil
properties and complaint clustering.
"""

NO_MEMORY_OVERRIDE = """
ABLATION MODE: do not call dossier_lookup or precedent_search. Do not
reference any prior verdict or precedent. Reason only from this complaint,
a fresh mireye_profile call, and moisture_history.
"""


def apply_ablation(base_instructions: str, ablation: str | None) -> str:
    if ablation == "no_moisture":
        return base_instructions + "\n\n" + NO_MOISTURE_OVERRIDE
    if ablation == "no_memory":
        return base_instructions + "\n\n" + NO_MEMORY_OVERRIDE
    return base_instructions

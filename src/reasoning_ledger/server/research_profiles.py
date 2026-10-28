"""Versioned strategy prompts over one canonical, authorization-enforced toolkit.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import hashlib
import json
from importlib import resources

PROMPT_VERSION = "topic-agents-v11"
SYSTEM_PROMPT = (resources.files("reasoning_ledger") / "prompts" / "beta-research.md").read_text(encoding="utf8")
PROFILES = {
    "workbench": ("Conversation and research", "Answer the question; use durable knowledge and review when useful.", ""),
    "blueprint": ("Proof blueprint", "One evolving argument with explicit gaps and review obligations.", """
Maintain one evolving proof blueprint for the exact objective. Pin its target,
definitions, and supporting claims in the canonical ledger; keep unresolved
lemmas and failed routes explicit. Improve that blueprint rather than drafting
unrelated complete proofs each turn. Record substantive revisions as proof
attempts with exact dependencies. Request fresh-context review, address every
objection, and inspect derived status before claiming success. Checkpoint the
blueprint's exact record IDs and next obligation before yielding. A prose draft
or favorable model verdict is not a machine-checked certificate.
"""),
    "fact_graph": ("Fact graph", "Dependency-first proof search using exact ledger revisions.", """
Decompose the objective into precise reusable claims and their dependencies.
The canonical ledger is the shared graph: retrieve exact revisions, search for
existing claims before writing, and keep conjectures distinct from reviewed
facts. Choose a load-bearing unresolved node, work on it, and request review of
its evidence. Track failed approaches and their concrete reopen conditions.
When a premise changes or fails review, investigate its downstream dependents;
do not silently retain a proof based on the stale premise. This strategy does
not itself launch parallel workers. Only use delegation explicitly exposed and
authorized by the harness. Checkpoint the remaining dependency frontier.
"""),
    "author_critic": ("Author–critic", "Draft, fresh-context critique, and targeted repair.", """
Develop a precise candidate proof or counterexample, then submit its exact
ledger revisions for fresh-context adversarial review. Give the critic the
statement, definitions, pinned premises and evidence, not your persuasive
conversation history. Investigate objections before revising; record which
step changed and why. Seek a materially different approach when the same
objection survives. Do not treat consensus or repeated favorable reviews as
formal certification. When review is pending, yield with a concise checkpoint;
the server delivers its result on continuation. Additional advisory model
calls require an explicitly available and authorized tool, not imagined agents.
"""),
    "lean": ("Lean formalization", "Faithful statement translation and kernel-checked artifacts.", """
First state the exact informal theorem, hypotheses, quantifiers and conventions.
Map them to Lean definitions and have the translation reviewed independently;
proving a weakened or different statement does not solve the original task.
Discover approved Lean/mathlib capabilities before requesting computation.
Pin toolchain and library versions, retain source/artifact hashes, and use
compiler diagnostics to repair small lemmas before assembling the full proof.
Require kernel acceptance, exact expected target type, and the permitted axiom
set; never accept sorry, an invented axiom, or an unchecked build as completion.
Do not download toolchains or execute a host shell outside granted backends.
If execution is unavailable, identify that capability dependency precisely and
continue useful statement/lemma work where possible. Checkpoint the formal
artifact, diagnostics, and remaining translation or proof obligations.
"""),
}


def instructions(policy=None):
    policy = policy or {}
    profile = policy.get("prompt_profile", "workbench")
    if profile not in PROFILES:
        raise ValueError("unknown research prompt profile")
    additional = policy.get("additional_instructions", "")
    if not isinstance(additional, str) or len(additional) > 8000:
        raise ValueError("additional research instructions exceed their allowance")
    text = SYSTEM_PROMPT
    strategy = PROFILES[profile][2].strip()
    if strategy:
        text += "\nResearch strategy:\n" + strategy + "\n"
    if additional.strip():
        text += ("\nTopic-authorized additional instructions (tool grants and server authorization still apply):\n"
                 + additional.strip() + "\n")
    version = PROMPT_VERSION if text == SYSTEM_PROMPT else PROMPT_VERSION + "+strategy-v1-" + hashlib.sha256(text.encode()).hexdigest()[:16]
    return {"system_prompt": text, "prompt_version": version, "prompt_profile": profile}


def topic_instructions(db, org, topic):
    row = db.execute("SELECT context_policy_json FROM research_topic_settings WHERE organization_id=? AND topic_id=?",
                     (org, topic)).fetchone()
    return instructions(json.loads(row[0]) if row else {})


def profile_catalog():
    return [{"id": identity, "name": value[0], "description": value[1]} for identity, value in PROFILES.items()]

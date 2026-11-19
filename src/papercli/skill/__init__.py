"""The portable agent skill (Phase 7, spec section 15).

``papers skill export --out DIR`` writes a standard SKILL.md + the
supporting references into DIR. Exporting the folder does NOT
install it anywhere: installation into an agent is explicit
(spec section 15).
"""

from papercli.skill.export import SKILL_NAME, export_skill, skill_files

__all__ = ["SKILL_NAME", "export_skill", "skill_files"]

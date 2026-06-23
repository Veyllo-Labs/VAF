"""
VAF Skills — Anthropic Agent Skills (SKILL.md) support.

A skill is a folder containing a SKILL.md file (YAML frontmatter + Markdown body)
plus optional bundled files. Skills are the second routing tier under workflows:
the router matches a skill by its name+description, and the full instructions are
loaded on demand via the use_skill tool (progressive disclosure).

Modules:
    skill_md   — SKILL.md parser (the format authority; pure parsing, no I/O policy)
    templates  — discovery + list_skills/reload_skills (mirror of workflows.templates)

The registry (manifest, scoping, zip import) lives at vaf.core.skills_registry,
mirroring vaf.core.custom_tools_registry.
"""

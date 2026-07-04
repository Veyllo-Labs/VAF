# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Keep pytest out of the coder scaffold templates.

The files under this package are project SCAFFOLDS containing ``{{PLACEHOLDER}}`` markers
(and per-scaffold ``test_*.py`` stubs). They are not tests of the VAF repository and only
become valid Python once the coder generates a project and substitutes the placeholders
(e.g. ``PORT = {{PORT}}`` is not importable raw). VAF's own suite runs ``pytest tests/``,
which never reaches here; this guard additionally protects a bare ``pytest`` invocation.
"""
collect_ignore_glob = ["*"]

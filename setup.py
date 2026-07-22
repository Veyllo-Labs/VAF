# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Legacy shim - all packaging metadata lives in pyproject.toml (PEP 621).

This file stays on disk only for legacy tooling that still expects a setup.py
and for documentation links pointing here. It must NOT grow metadata or
install-time behavior: the platform installers (install.sh / install.ps1) do
their own provisioning, and a plain `pip install` must never mutate the host.
"""

from setuptools import setup

setup()

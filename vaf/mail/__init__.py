# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""VAF mail client engine (v2).

Design doc: docs/integrations/EMAIL_CLIENT.md (read it before changing anything
here). One SQLite store per user scope, RFC 4549 baseline sync, JWZ-style
conversation threading, FTS5 search, encrypted raw bodies (decision E4).

External dependencies (IMAPClient, aiosmtplib, nh3, zstandard) are imported
lazily inside the modules that need them so the slim library base stays
importable without the "mail" extra.
"""

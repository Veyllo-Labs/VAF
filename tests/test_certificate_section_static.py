# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The certificate section of the settings, pinned at the source (web/ has no JS test runner).

The report codes are one list with three copies: `REPORT_REASONS` in
vaf/network/ssl_utils.py (the English text the CLI prints), `REASON_CODES` in the section
(which codes it words), and the `reasons` block of the catalogues (the words). A code the
section does not know shows the server's English text, so a code added in one place and
forgotten in another stays readable but untranslated; this guard says so first.
"""
import json
import re
from pathlib import Path

from vaf.network.ssl_utils import REPORT_REASONS

ROOT = Path(__file__).resolve().parents[1]
SECTION = ROOT / "web" / "components" / "settings" / "CertificateSection.tsx"
MODAL = ROOT / "web" / "components" / "SettingsModal.tsx"


def _source() -> str:
    return SECTION.read_text(encoding="utf-8")


def test_the_report_codes_are_one_list():
    """MUTATION: drop a code from REASON_CODES - red."""
    block = re.search(r"const REASON_CODES = \[([\s\S]*?)\];", _source())
    assert block, "REASON_CODES moved or was renamed"
    section = set(re.findall(r"'([a-z_]+)'", block.group(1)))
    catalog = json.loads((ROOT / "web" / "messages" / "en.json").read_text(encoding="utf-8"))
    worded = set(catalog["settings"]["localNetwork"]["certificate"]["reasons"])
    assert section == set(REPORT_REASONS) == worded


def test_not_an_admin_hides_the_section():
    """Only "not yours" (401/403) hides it; any other failure keeps it with a retry.
    MUTATION: drop the 401/403 branch - red."""
    load = _source()[_source().index("const load = useCallback"):]
    load = load[:load.index("}, [apiBase]);")]
    assert "res.status === 401 || res.status === 403" in load and "setData(null)" in load
    assert "setLoadFailed(true)" in load


def test_a_change_waits_for_the_code_when_one_is_asked():
    """A signed-in admin gives the 2FA code before applying, resetting or renewing.
    MUTATION: drop codeReady from the apply condition - red."""
    source = _source()
    assert "const codeReady = !data.step_up_required || /^\\d{6}$/.test(code.trim());" in source
    can_apply = re.search(r"const canApply = ([^;]+);", source).group(1)
    assert "codeReady" in can_apply and "report.ok" in can_apply
    assert source.count("disabled={busy || !codeReady}") == 2, "restrict and reset wait too"


def test_the_download_is_offered_only_for_vafs_own_authority():
    """/api/a2a/ca.pem serves the authority of the SERVED chain: with an own certificate that
    is the own chain's top, not the local CA whose fingerprint the section shows beside the
    link. MUTATION: offer the download in every state - red."""
    source = _source()
    link = source.index("/api/a2a/ca.pem")
    guard = source.rfind("data.source === 'automatic' &&", 0, link)
    assert guard != -1 and link - guard < 400


def test_the_section_follows_remote_access_in_the_local_network_tab():
    modal = MODAL.read_text(encoding="utf-8")
    remote = modal.index("<RemoteAccessSection sectionId=")
    cert = modal.index("<CertificateSection sectionId=")
    users = modal.index("<Section title={tLocalNet('userManagement')}>")
    assert remote < cert < users
    assert "tLocalNet('certificate.title')" in modal[:remote], "the settings search lists it"

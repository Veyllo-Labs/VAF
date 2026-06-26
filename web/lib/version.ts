// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

// Derive the user-facing version badge from the single source of truth: the backend
// `vaf/version.py` __version__, served at GET /api/version. Bumping the backend version
// auto-updates the badge — nothing here is hardcoded. (web/package.json is NOT used.)

export interface VersionInfo {
  /** e.g. "v2.6" (major.minor), or "" when the raw version is unparseable. */
  display: string;
  /** e.g. "Open Alpha" | "Beta" | "Release Candidate", or null for a stable release. */
  channel: string | null;
}

/** Parse a PEP440-ish version ("2.6.0a0", "2.7.0b1", "2.8.0rc1", "2.6.0", "2.6 Alpha"). */
export function formatVersion(raw?: string | null): VersionInfo {
  const s = (raw ?? '').trim();
  const mm = s.match(/(\d+)\.(\d+)/);
  if (!mm) return { display: '', channel: null };
  const display = `v${mm[1]}.${mm[2]}`;

  // Whatever follows the numeric release (e.g. "a0", "b1", "rc1", " Alpha").
  const rel = s.match(/^\D*\d+(?:\.\d+)*(.*)$/);
  const suffix = (rel ? rel[1] : '').trim();

  let channel: string | null = null;
  if (/^[._\-\s]*rc/i.test(suffix) || /release[\s\-]?candidate/i.test(s)) channel = 'Release Candidate';
  else if (/^[._\-\s]*a(lpha)?/i.test(suffix) || /alpha/i.test(s)) channel = 'Open Alpha';
  else if (/^[._\-\s]*b(eta)?/i.test(suffix) || /beta/i.test(s)) channel = 'Beta';
  return { display, channel };
}

/** The comparable + persisted form: "2.6" (major.minor), or null when unparseable. */
export function parseMajorMinor(raw?: string | null): string | null {
  const mm = (raw ?? '').match(/(\d+)\.(\d+)/);
  return mm ? `${mm[1]}.${mm[2]}` : null;
}

/** Compare two major.minor versions: >0 if a is newer, <0 if older, 0 if equal/both unknown. */
export function compareMajorMinor(a?: string | null, b?: string | null): number {
  const pa = parseMajorMinor(a);
  const pb = parseMajorMinor(b);
  if (!pa && !pb) return 0;
  if (!pa) return -1;
  if (!pb) return 1;
  const [a1, a2] = pa.split('.').map(Number);
  const [b1, b2] = pb.split('.').map(Number);
  return a1 !== b1 ? a1 - b1 : a2 - b2;
}

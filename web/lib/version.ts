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

// --- Full-version comparison (PEP 440-ish) -----------------------------------------------
// major.minor granularity cannot distinguish the alpha releases (0.1.0a1 vs 0.1.0a2 are both
// "0.1"), so the announcement/changelog logic uses these. (The former parseMajorMinor /
// compareMajorMinor helpers were removed with the switch; nothing else consumed them.)

interface ParsedVersion {
  release: number[];        // e.g. [0, 1, 0]
  phaseRank: number;        // legacy-bare < a < b < rc < final
  pre: number;              // the N in a/b/rc N
}

const PHASE_RANKS: Record<string, number> = { a: 1, alpha: 1, b: 2, beta: 2, rc: 3 };

/** Parse "0.1.0a2" / "v0.2.1rc1" / "1.0" into a comparable structure (null if unparseable).
 *
 * A BARE major.minor with no suffix (exactly two components, e.g. "0.1") is treated as the
 * very beginning of that series - OLDER than any concrete "0.1.x" version. That is the shape
 * older builds persisted as last_seen_announcement_version, and those users must be shown
 * the next changelog (not the intro) without bare "0.1" outranking the 0.1.0aX prereleases
 * (under strict PEP 440 a final 0.1 would be NEWER than 0.1.0a2). */
export function parseVersion(raw?: string | null): ParsedVersion | null {
  const s = (raw ?? '').trim().toLowerCase();
  const m = s.match(/^v?(\d+(?:\.\d+)*)\s*(?:[._-]?\s*(a|alpha|b|beta|rc)\s*[._-]?\s*(\d+)?)?/);
  if (!m || !m[1]) return null;
  const release = m[1].split('.').map(Number);
  const legacyBare = release.length === 2 && !m[2];
  const phaseRank = legacyBare ? 0 : (m[2] ? PHASE_RANKS[m[2]] : 4);
  const pre = m[3] !== undefined ? Number(m[3]) : 0;
  return { release, phaseRank, pre };
}

/** Compare two full versions: >0 if a is newer, <0 if older, 0 if equal/both unparseable. */
export function compareVersions(a?: string | null, b?: string | null): number {
  const pa = parseVersion(a);
  const pb = parseVersion(b);
  if (!pa && !pb) return 0;
  if (!pa) return -1;
  if (!pb) return 1;
  const len = Math.max(pa.release.length, pb.release.length);
  for (let i = 0; i < len; i++) {
    const d = (pa.release[i] ?? 0) - (pb.release[i] ?? 0);
    if (d !== 0) return d;
  }
  if (pa.phaseRank !== pb.phaseRank) return pa.phaseRank - pb.phaseRank;
  return pa.pre - pb.pre;
}

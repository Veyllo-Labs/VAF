// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

// The product changelog, shown in the announcement modal's "What's new" variant.
// Item text is DATA (edited here per release); the section labels + the intro disclaimer
// are i18n (messages/*.json -> modals.announcement). To ship a new "what's new": in the SAME
// commit that bumps vaf/version.py, prepend an entry here whose `version` matches the new
// FULL version exactly (e.g. '0.1.0a3') — the modal fires when the latest entry is newer
// than what the user last acknowledged, so version and entry must move together.

import { compareVersions, parseVersion } from './version';

export type ChangeKind = 'new' | 'improved' | 'fixed' | 'removed';

export interface ChangelogSection {
  kind: ChangeKind;
  items: string[];
}

export interface ChangelogEntry {
  /** FULL version, e.g. "0.1.0a3" — must match vaf/version.py of the release shipping it. */
  version: string;
  /** ISO date, e.g. "2026-06-26". */
  date: string;
  sections: ChangelogSection[];
}

// Newest first. Only the LATEST entry is ever shown (one "what's new" per update, not a
// history); older entries stay here as the in-app record. Keep items user-facing and short —
// the full technical record lives in /CHANGELOG.md.
export const CHANGELOG: ChangelogEntry[] = [
  {
    version: '0.1.0a5',
    date: '2026-07-04',
    sections: [
      {
        kind: 'new',
        items: [
          'The coding agent can now run your project’s tests and see the real pass/fail, instead of guessing.',
          'The coding agent writes a README for what it builds — and updates an existing one when it changes your project.',
          'Coding tasks on an existing project no longer stall: the agent reads the project before it plans.',
        ],
      },
      {
        kind: 'improved',
        items: [
          'The coding agent’s shell now runs in a locked-down workspace, so a generated build can’t touch VAF itself; host and docker commands run only with your explicit confirmation.',
          'Project templates now start from a small working example with a passing test, so results are more reliable.',
        ],
      },
      {
        kind: 'fixed',
        items: [
          'Created Markdown and text files open in the built-in viewer with a preview toggle.',
          'The failover level selector no longer draws its connecting line through the unselected dots.',
        ],
      },
    ],
  },
  {
    version: '0.1.0a4',
    date: '2026-07-04',
    sections: [
      {
        kind: 'fixed',
        items: [
          'Files an automation or workflow creates now stay in your chat’s workspace instead of ending up in your home folder.',
          'Opening a created file no longer replaces the whole window with a raw error page — it opens in the viewer, and downloads show a clear message if something goes wrong.',
          'Security: refreshed bundled dependencies (all critical and high advisories resolved).',
        ],
      },
    ],
  },
  {
    version: '0.1.0a3',
    date: '2026-07-03',
    sections: [
      {
        kind: 'new',
        items: [
          'Update notes: after each update, this window shows you what changed.',
        ],
      },
      {
        kind: 'improved',
        items: [
          'Apple Silicon: the automatic model choice now scales with your Mac’s memory instead of always picking the smallest model.',
        ],
      },
      {
        kind: 'fixed',
        items: [
          'Windows: installing on a machine without WSL2 no longer fails at the container-runtime step — the installer enables WSL2 up front (one approval prompt) and pauses cleanly for the required restart.',
          'First-run setup no longer gets stuck on a login form while the database is still starting — the setup wizard now appears on its own.',
          'The local model loads reliably: no more endless restart loops during model loading, plus a compatibility fallback for models without Flash Attention support.',
          'macOS: the service stack starts even when “docker compose” is broken on the machine (automatic fallback to the legacy docker-compose).',
          'macOS: microphone voice input works in the desktop window (approve the one-time system prompt).',
        ],
      },
    ],
  },
];

export function latestEntry(): ChangelogEntry | null {
  return CHANGELOG.length ? CHANGELOG[0] : null;
}

export type AnnouncementKind = 'intro' | 'changelog' | null;

// Which announcement (if any) to show this load:
//  - never acknowledged anything            -> intro (the Open Alpha welcome)
//  - latest changelog entry newer than seen -> changelog
//  - otherwise                              -> nothing
// Comparison is FULL-version aware (0.1.0a1 < 0.1.0a2); a legacy persisted "0.1"
// (older builds stored major.minor) counts as the beginning of the 0.1 series, so
// those users see the next changelog once without the intro re-firing.
export function decideAnnouncement(
  lastSeen: string | null | undefined,
  currentRaw?: string | null,
): AnnouncementKind {
  if (!lastSeen) return 'intro';
  const latest = latestEntry();
  if (!latest) return null;
  // Pre-reset artifact: VAF's internal versions (2.x) were renumbered to 0.1.x for the
  // public alpha, so early installs persisted acks like "2.7" that outrank every real
  // version forever and mute the notes. A stored ack whose RELEASE part is newer than the
  // running app can only be such an artifact (a prerelease-only downgrade like a3 -> a2
  // differs only in the suffix and does not trigger this) - treat it like "never saw a
  // changelog": show the latest notes once; acknowledging overwrites the stale value.
  const seen = parseVersion(lastSeen);
  const cur = parseVersion(currentRaw);
  if (seen && cur) {
    const len = Math.max(seen.release.length, cur.release.length);
    for (let i = 0; i < len; i++) {
      const d = (seen.release[i] ?? 0) - (cur.release[i] ?? 0);
      if (d > 0) return 'changelog';
      if (d < 0) break;
    }
  }
  if (compareVersions(latest.version, lastSeen) > 0) return 'changelog';
  return null;
}

// The version to persist when the user dismisses: the newer of the running app version and
// the latest changelog entry (as FULL version strings), so neither the intro nor the
// changelog can re-fire afterwards.
export function acknowledgedVersion(currentRaw?: string | null): string {
  const app = (currentRaw ?? '').trim() || null;
  const latest = latestEntry()?.version ?? null;
  if (app && latest) return compareVersions(app, latest) >= 0 ? app : latest;
  return app ?? latest ?? '';
}

// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

// The product changelog, shown in the announcement modal's "What's new" variant.
// Item text is DATA (edited here per release); the section labels + the intro disclaimer
// are i18n (messages/*.json -> modals.announcement). To ship a new "what's new": in the SAME
// commit that bumps vaf/version.py, prepend an entry here whose `version` matches the new
// FULL version exactly (e.g. '0.1.0a3') — the modal fires when the latest entry is newer
// than what the user last acknowledged, so version and entry must move together.

import { compareVersions } from './version';

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
    // NOTE: finalize version + date when the release is tagged (see header comment).
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
  _currentRaw?: string | null,
): AnnouncementKind {
  if (!lastSeen) return 'intro';
  const latest = latestEntry();
  if (latest && compareVersions(latest.version, lastSeen) > 0) return 'changelog';
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

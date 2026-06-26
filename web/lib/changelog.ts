// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

// The product changelog, shown in the announcement modal's "Was ist neu" variant.
// Item text is DATA (edited here per release); the section labels + the intro disclaimer
// are i18n (messages/*.json -> modals.announcement). To ship a new "what's new": bump the
// backend version (vaf/version.py) AND prepend an entry here whose `version` matches.

import { compareMajorMinor, parseMajorMinor } from './version';

export type ChangeKind = 'new' | 'improved' | 'fixed' | 'removed';

export interface ChangelogSection {
  kind: ChangeKind;
  items: string[];
}

export interface ChangelogEntry {
  /** major.minor, e.g. "2.7" — tracks the app version shipped with this entry. */
  version: string;
  /** ISO date, e.g. "2026-06-26". */
  date: string;
  sections: ChangelogSection[];
}

// Newest first.
export const CHANGELOG: ChangelogEntry[] = [
  {
    version: '2.7',
    date: '2026-06-26',
    sections: [
      {
        kind: 'new',
        items: [
          'Eigene Skills direkt im Chat anlegen und bearbeiten.',
          'Automatisierungen laufen still im Hintergrund.',
        ],
      },
      { kind: 'improved', items: ['Schnellere Antworten bei mehreren Nutzern gleichzeitig.'] },
      { kind: 'fixed', items: ['Hintergrund-Agenten leaken keine Tool-Aktivität mehr in fremde Chats.'] },
      { kind: 'removed', items: ['Alter Experiment-Schalter „X“ (wird nicht weiterentwickelt).'] },
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
export function decideAnnouncement(
  lastSeen: string | null | undefined,
  _currentRaw?: string | null,
): AnnouncementKind {
  if (!lastSeen) return 'intro';
  const latest = latestEntry();
  if (latest && compareMajorMinor(latest.version, lastSeen) > 0) return 'changelog';
  return null;
}

// The version to persist when the user dismisses: the newer of the running app version and the
// latest changelog entry, so neither the intro nor the changelog can re-fire afterwards.
export function acknowledgedVersion(currentRaw?: string | null): string {
  const app = parseMajorMinor(currentRaw);
  const latest = latestEntry()?.version ?? null;
  if (app && latest) return compareMajorMinor(app, latest) >= 0 ? app : latest;
  return app ?? latest ?? '';
}

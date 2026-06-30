// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md

// The product changelog, shown in the announcement modal's "What's new" variant.
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
  /** major.minor, e.g. "0.2" — tracks the app version shipped with this entry. */
  version: string;
  /** ISO date, e.g. "2026-06-26". */
  date: string;
  sections: ChangelogSection[];
}

// Newest first. Empty for the 0.1 Open Alpha: there is no prior public release to diff against,
// so first-run users get the 'intro' welcome (decideAnnouncement), not a "what's new" list. At the
// next release with user-visible changes, prepend an entry here — in English, per the docs language
// policy — whose `version` is the new major.minor (e.g. '0.2').
export const CHANGELOG: ChangelogEntry[] = [];

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

// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * The in-memory per-chat message cache: its bounds, and the removal of the localStorage copy
 * older versions wrote. Chat messages are never written to browser storage.
 */

// The key older versions stored the cache under; only ever removed now.
const STORAGE_KEY = "vaf_session_cache_v1";

const MAX_SESSIONS = 50;
const MAX_MESSAGES_PER_SESSION = 150;

export type SessionCacheMessageRole = 'user' | 'assistant' | 'system' | 'tool' | 'workflow';

export type SessionCacheMessage = {
  role: SessionCacheMessageRole;
  content: string;
  timestamp: number;
  [key: string]: unknown;
};

export type SessionCache = Record<string, SessionCacheMessage[]>;

export type TrimOptions = {
  currentSessionId: string | null;
  sessionIdsInOrder: string[];
};

/**
 * Trim cache to limits: keep at most MAX_MESSAGES_PER_SESSION per session,
 * and at most MAX_SESSIONS (prioritising currentSessionId then sessionIdsInOrder).
 * Returns a new object; does not mutate input.
 */
export function trimSessionCache(
  cache: SessionCache,
  options: TrimOptions,
  limits?: { maxSessions: number; maxMessagesPerSession: number }
): SessionCache {
  const maxSessions = limits?.maxSessions ?? MAX_SESSIONS;
  const maxMessages = limits?.maxMessagesPerSession ?? MAX_MESSAGES_PER_SESSION;

  const { currentSessionId, sessionIdsInOrder } = options;
  const orderSet = new Set(sessionIdsInOrder);
  const ordered: string[] = [];
  if (currentSessionId && cache[currentSessionId]) {
    ordered.push(currentSessionId);
  }
  for (const id of sessionIdsInOrder) {
    if (id !== currentSessionId && cache[id]) ordered.push(id);
  }
  for (const id of Object.keys(cache)) {
    if (!orderSet.has(id)) ordered.push(id);
  }
  const toKeep = ordered.slice(0, maxSessions);

  const out: SessionCache = {};
  for (const id of toKeep) {
    const msgs = cache[id];
    if (!Array.isArray(msgs)) continue;
    const trimmed = msgs.slice(-maxMessages);
    if (trimmed.length > 0) out[id] = trimmed;
  }
  return out;
}

/**
 * Remove the copy of chat messages an older version kept in localStorage. The cache is held
 * in memory now: the stored copy was keyed by chat id alone and outlived a logout, so on a
 * shared browser a second account could see the first one's chats before the server refused
 * them. Never throws (storage may be blocked or missing).
 */
export function purgeLegacySessionCache(): void {
  try {
    if (typeof window === "undefined" || !window.localStorage) return;
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage blocked: nothing was stored either */
  }
}

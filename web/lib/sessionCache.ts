// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * Session cache persistence with LocalStorage quota limits.
 * Trims by session count, messages per session, and total bytes; retries on QuotaExceededError.
 */

const STORAGE_KEY = "vaf_session_cache_v1";

const MAX_CACHE_BYTES = 4 * 1024 * 1024; // 4 MB
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
 * Load cache from LocalStorage. Returns {} on missing, oversized, or invalid data.
 * Result is trimmed to limits.
 */
export function loadSessionCache(): SessionCache {
  if (typeof window === "undefined" || !window.localStorage) return {};
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === null) return {};
    if (saved.length > MAX_CACHE_BYTES) return {};
    const parsed = JSON.parse(saved) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const cache = parsed as SessionCache;
    const keys = Object.keys(cache);
    return trimSessionCache(cache, {
      currentSessionId: null,
      sessionIdsInOrder: keys,
    });
  } catch {
    return {};
  }
}

function isQuotaError(e: unknown): boolean {
  if (e instanceof DOMException && e.name === "QuotaExceededError") return true;
  if (e instanceof Error && /quota|storage/i.test(e.message)) return true;
  return false;
}

/**
 * Trim cache, then persist. If payload exceeds MAX_CACHE_BYTES, trims more aggressively.
 * On QuotaExceededError, retries once with halved limits. Does not overwrite with {} on failure.
 */
export function saveSessionCache(cache: SessionCache, options: TrimOptions): void {
  if (typeof window === "undefined" || !window.localStorage) return;

  let trimmed = trimSessionCache(cache, options);
  let payload = JSON.stringify(trimmed);

  if (payload.length > MAX_CACHE_BYTES) {
    const halfSessions = Math.max(1, Math.floor(MAX_SESSIONS / 2));
    const halfMessages = Math.max(10, Math.floor(MAX_MESSAGES_PER_SESSION / 2));
    trimmed = trimSessionCache(cache, options, {
      maxSessions: halfSessions,
      maxMessagesPerSession: halfMessages,
    });
    payload = JSON.stringify(trimmed);
  }

  try {
    localStorage.setItem(STORAGE_KEY, payload);
  } catch (e) {
    if (!isQuotaError(e)) {
      console.warn("Failed to save session cache.", e);
      return;
    }
    const halfSessions = Math.max(1, Math.floor(MAX_SESSIONS / 2));
    const halfMessages = Math.max(10, Math.floor(MAX_MESSAGES_PER_SESSION / 2));
    const retryTrimmed = trimSessionCache(cache, options, {
      maxSessions: halfSessions,
      maxMessagesPerSession: halfMessages,
    });
    const retryPayload = JSON.stringify(retryTrimmed);
    try {
      localStorage.setItem(STORAGE_KEY, retryPayload);
    } catch (retryErr) {
      console.warn("LocalStorage quota exceeded, failed to save session cache after retry.", retryErr);
    }
  }
}

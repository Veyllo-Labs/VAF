// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

/**
 * Strip <think>...</think> blocks (and a trailing unclosed <think>) from an
 * assistant message. For views that show a transcript as the conversation the
 * user actually received: think content is never delivered to the channel.
 */
export function stripThinkBlocks(text: string): string {
  return text
    .replace(/<think>[\s\S]*?<\/think>/gi, "")
    .replace(/<think>[\s\S]*$/i, "")
    .trim();
}

/**
 * API base URL for fetch calls.
 * Returns empty string to use the Next.js proxy (/api/...) on the same port.
 * This avoids CORS and SSL issues by using the internal Port 8005 channel.
 */
export function getApiBase(): string {
  return "";
}

/** 
 * WebSocket base URL. 
 * Needs absolute URL because WebSockets cannot be easily proxied by Next.js rewrites.
 */
export function getWsBase(): string {
  if (typeof window !== "undefined") {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    // If we are on port 3000 (standard frontend), we connect to 8001
    // If we are already on 8001 (e.g. through a reverse proxy), we stay on 8001
    const port = window.location.port === "3000" ? "8001" : (window.location.port || "8001");
    return `${protocol}://${window.location.hostname}:${port}`;
  }
  return "ws://localhost:8001";
}

/**
 * Direct backend origin for the rare fetch that must BYPASS the Next.js proxy,
 * or null when same-origin already IS the backend. Same port derivation as
 * getWsBase (3000 -> 8001; any other port means a proxy fronts both).
 *
 * Only useful for unauthenticated endpoints (/api/version): the auth cookie is
 * SameSite=Lax, so a cross-origin fetch carries no session. The update dialog
 * uses this while the Next server is down for its post-update rebuild - the
 * backend is up and answering minutes before the proxy is.
 */
export function getApiDirectBase(): string | null {
  if (typeof window === "undefined") return null;
  if (window.location.port !== "3000") return null;
  const protocol = window.location.protocol === "https:" ? "https" : "http";
  return `${protocol}://${window.location.hostname}:8001`;
}

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

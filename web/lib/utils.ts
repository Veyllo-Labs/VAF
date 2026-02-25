import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

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

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

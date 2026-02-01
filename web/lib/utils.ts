import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

/** API base URL: same host as current page with port 8001 (so localhost works after Local Network is disabled). */
export function getApiBase(): string {
  if (typeof window !== "undefined") {
    return `${window.location.protocol}//${window.location.hostname}:8001`
  }
  return process.env.NEXT_PUBLIC_API_URL || "http://localhost:8001"
}

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

/**
 * Enforce a single public entry point for WebUI usage.
 * Optional: if frontend is accessed on :3000, redirect to HTTPS proxy on :8443.
 * Guarded by env flag to avoid locking users out when 8443 is not active.
 */
export function proxy(request: NextRequest) {
  const enforce8443 = process.env.VAF_ENFORCE_8443_ONLY === '1';
  if (!enforce8443) {
    return NextResponse.next();
  }

  const host = request.headers.get('host') || '';
  if (!host.endsWith(':3000')) {
    return NextResponse.next();
  }

  const url = request.nextUrl.clone();
  url.protocol = 'https:';
  url.port = '8443';
  return NextResponse.redirect(url, 307);
}

export const config = {
  matcher: '/:path*',
};


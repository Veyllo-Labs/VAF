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
    return applyAuthGuards(request);
  }

  const host = request.headers.get('host') || '';
  if (host.endsWith(':3000')) {
    const url = request.nextUrl.clone();
    url.protocol = 'https:';
    url.port = '8443';
    return NextResponse.redirect(url, 307);
  }

  return applyAuthGuards(request);
}

/**
 * Edge-safe validity check: decode the JWT payload (base64url) and treat a missing, malformed,
 * or EXPIRED token as unauthenticated. No signature verification here — the backend stays the real
 * authority on /api/auth/me and /ws; this guard only needs to agree with it on expiry. Checking
 * mere cookie *presence* let an expired-but-present cookie drive an infinite /login ↔ / loop.
 */
function isTokenValid(token: string | undefined): boolean {
  if (!token) return false;
  const parts = token.split('.');
  if (parts.length !== 3) return false;
  try {
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4);
    const payload = JSON.parse(atob(padded));
    if (typeof payload.exp !== 'number') return false;
    return payload.exp * 1000 > Date.now();
  } catch {
    return false;
  }
}

function applyAuthGuards(request: NextRequest): NextResponse {
  const { pathname, search } = request.nextUrl;
  const token = request.cookies.get('vaf_token')?.value?.trim();
  const isAuthenticated = isTokenValid(token);
  // A present-but-expired/invalid token must be actively cleared here: the browser cannot delete
  // the httpOnly cookie itself, and a lingering stale cookie would keep this guard out of sync
  // with the backend (the original cause of the /login ↔ / redirect loop).
  const hasStaleToken = Boolean(token) && !isAuthenticated;
  const isLoginRoute = pathname === '/login';
  // Test/preview hook (DEV ONLY): /login?preview=… renders an onboarding step on demand, so an
  // authenticated user must NOT be bounced to / there (see web/app/login/page.tsx). Gated to the
  // dev build; in production NODE_ENV is 'production' so this is false and the hook is inert.
  const isPreview = process.env.NODE_ENV === 'development' && request.nextUrl.searchParams.has('preview');

  // Never enforce auth for API/static assets.
  const isPublicAsset =
    pathname.startsWith('/api/') ||
    pathname.startsWith('/_next/') ||
    pathname.startsWith('/sounds/') ||
    pathname.startsWith('/favicon') ||
    pathname === '/logo.png' ||
    pathname === '/robots.txt' ||
    pathname === '/sitemap.xml';

  if (isPublicAsset) {
    return NextResponse.next();
  }

  if (isLoginRoute && isAuthenticated && !isPreview) {
    const url = request.nextUrl.clone();
    url.pathname = '/';
    url.search = '';
    return NextResponse.redirect(url, 307);
  }

  if (!isLoginRoute && !isAuthenticated) {
    const url = request.nextUrl.clone();
    url.pathname = '/login';
    const nextPath = `${pathname}${search || ''}`;
    if (nextPath && nextPath !== '/') {
      url.searchParams.set('next', nextPath);
    } else {
      url.search = '';
    }
    const res = NextResponse.redirect(url, 307);
    if (hasStaleToken) res.cookies.set('vaf_token', '', { path: '/', maxAge: 0 });
    return res;
  }

  // Login route while unauthenticated: render the login page, dropping any stale cookie.
  const res = NextResponse.next();
  if (hasStaleToken) res.cookies.set('vaf_token', '', { path: '/', maxAge: 0 });
  return res;
}

export const config = {
  matcher: '/:path*',
};


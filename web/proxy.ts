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

function applyAuthGuards(request: NextRequest): NextResponse {
  const { pathname, search } = request.nextUrl;
  const token = request.cookies.get('vaf_token')?.value?.trim();
  const isAuthenticated = Boolean(token);
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
    return NextResponse.redirect(url, 307);
  }

  return NextResponse.next();
}

export const config = {
  matcher: '/:path*',
};


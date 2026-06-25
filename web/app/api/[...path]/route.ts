// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * Proxy all /api/* requests to the VAF backend with proper cookie forwarding.
 * Fixes 401 (Unauthorized) when Next.js rewrites did not forward cookies reliably.
 */

const BACKEND_PORT = process.env.VAF_INTERNAL_API_PORT || '8005';
const BACKEND_ORIGIN = `http://127.0.0.1:${BACKEND_PORT}`;

function buildBackendUrl(path: string[], searchParams: string): string {
  const pathStr = path.length > 0 ? path.join('/') : '';
  const query = searchParams ? `?${searchParams}` : '';
  return `${BACKEND_ORIGIN}/api/${pathStr}${query}`;
}

function copyForwardHeaders(request: Request): HeadersInit {
  const out: Record<string, string> = {};
  const toForward = ['cookie', 'content-type', 'authorization', 'accept', 'accept-language'];
  for (const name of toForward) {
    const v = request.headers.get(name);
    if (v) out[name] = v;
  }
  return out;
}

function copyResponseHeaders(backendResponse: Response): Headers {
  const h = new Headers();
  backendResponse.headers.forEach((value, key) => {
    // Forward all headers including set-cookie (may be multiple)
    h.append(key, value);
  });
  return h;
}

async function proxy(request: Request, path: string[]): Promise<Response> {
  const url = new URL(request.url);
  const backendUrl = buildBackendUrl(path, url.searchParams.toString());
  const headers = copyForwardHeaders(request);
  const method = request.method;
  const body = method !== 'GET' && method !== 'HEAD' ? await request.arrayBuffer() : undefined;
  const backendResponse = await fetch(backendUrl, {
    method,
    headers,
    body,
    cache: 'no-store',
  });
  return new Response(backendResponse.body, {
    status: backendResponse.status,
    statusText: backendResponse.statusText,
    headers: copyResponseHeaders(backendResponse),
  });
}

export async function GET(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxy(request, path);
}

export async function POST(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxy(request, path);
}

export async function PUT(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxy(request, path);
}

export async function PATCH(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxy(request, path);
}

export async function DELETE(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxy(request, path);
}

export async function HEAD(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxy(request, path);
}

export async function OPTIONS(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxy(request, path);
}

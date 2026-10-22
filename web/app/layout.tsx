// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import "./globals.css";
import HostnameNormalizer from "@/components/HostnameNormalizer";
import IntlProviderWrapper from "@/components/IntlProviderWrapper";
import { CustomCursor } from "@/components/CustomCursor";

export const metadata: Metadata = {
  title: "VAF Dashboard",
  description: "Veyllo Agentic Framework Control Center",
};

// Mobile: render at the device width (not a zoomed-out desktop page) and extend
// under the notch / home-indicator so safe-area insets work. Zoom stays enabled (a11y).
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  interactiveWidget: "resizes-content",
};

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // The page's nonce (web/proxy.ts). Reading the request headers also renders every page per
  // request, which is when Next.js puts the nonce on its own scripts: a page rendered at build
  // time has no request and no nonce. A self-hosted app with a handful of users can afford
  // that. The one inline script below carries it too.
  const nonce = (await headers()).get("x-nonce") || undefined;
  return (
    <html lang="de" className="light" suppressHydrationWarning>
      <body className="antialiased min-h-screen bg-background text-foreground">
        {/* Theme and language pre-paint, before first paint.
            Parser-blocking on purpose, because globals.css applies a global
            transition-colors, so a post-hydration class change would visibly
            fade light->dark on every load. App Router forbids a custom <head>,
            so this runs as the first child of <body> (next-themes pattern).
            The lang stamp matters for CJK: Han unification picks the glyph
            shapes from the declared language, so painting Japanese or Chinese
            under lang="de" until hydration shows the wrong forms of shared
            kanji. IntlProviderWrapper still owns the value; this only gets it
            right one frame earlier, and a code the app does not support is
            harmless because the wrapper overwrites it on mount. */}
        <script
          nonce={nonce}
          dangerouslySetInnerHTML={{
            __html:
              "try{if(localStorage.getItem('vaf_theme')==='dark'){var c=document.documentElement.classList;c.remove('light');c.add('dark')}}catch(e){}"
              + "try{var l=localStorage.getItem('ui_locale')||(navigator.language||'').split('-')[0].toLowerCase();if(/^[a-z]{2}$/.test(l))document.documentElement.lang=l}catch(e){}",
          }}
        />
        <CustomCursor />
        <HostnameNormalizer />
        <IntlProviderWrapper>
          {children}
        </IntlProviderWrapper>
      </body>
    </html>
  );
}

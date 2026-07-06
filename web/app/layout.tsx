// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
import type { Metadata, Viewport } from "next";
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

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="de" className="light" suppressHydrationWarning>
      <body className="antialiased min-h-screen bg-background text-foreground">
        {/* Theme pre-paint: stamp the persisted dark class BEFORE first paint.
            Parser-blocking on purpose — globals.css applies a global
            transition-colors, so a post-hydration class change would visibly
            fade light->dark on every load. App Router forbids a custom <head>,
            so this runs as the first child of <body> (next-themes pattern). */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              "try{if(localStorage.getItem('vaf_theme')==='dark'){var c=document.documentElement.classList;c.remove('light');c.add('dark')}}catch(e){}",
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

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
        <CustomCursor />
        <HostnameNormalizer />
        <IntlProviderWrapper>
          {children}
        </IntlProviderWrapper>
      </body>
    </html>
  );
}

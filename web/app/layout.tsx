import type { Metadata } from "next";
import "./globals.css";
import HostnameNormalizer from "@/components/HostnameNormalizer";
import IntlProviderWrapper from "@/components/IntlProviderWrapper";
import { CustomCursor } from "@/components/CustomCursor";

export const metadata: Metadata = {
  title: "VAF Dashboard",
  description: "Veyllo Agentic Framework Control Center",
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

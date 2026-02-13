import type { Metadata } from "next";
import "./globals.css";
import HostnameNormalizer from "@/components/HostnameNormalizer";

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
    <html lang="en" className="light" suppressHydrationWarning>
      <body className="antialiased min-h-screen bg-background text-foreground">
        <HostnameNormalizer />
        {children}
      </body>
    </html>
  );
}

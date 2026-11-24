import type { Metadata, Viewport } from "next";
import "./globals.css";
import { ConfirmProvider } from "@/components/confirm";

export const metadata: Metadata = {
  title: { default: "scraper0", template: "%s · scraper0" },
  description: "Turn any website, poster or spreadsheet into structured data.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fafafa" },
    { media: "(prefers-color-scheme: dark)", color: "#09090b" },
  ],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-dvh bg-bg text-fg">
        <ConfirmProvider>{children}</ConfirmProvider>
      </body>
    </html>
  );
}

import type { Metadata, Viewport } from "next";
import "./globals.css";
import { NavBar } from "@/components/NavBar";
import { PwaSetup } from "@/components/PwaSetup";

export const metadata: Metadata = {
  title: "Trading Desk",
  appleWebApp: { capable: true, statusBarStyle: "black-translucent", title: "Desk" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <PwaSetup />
        <NavBar />
        <main>{children}</main>
      </body>
    </html>
  );
}

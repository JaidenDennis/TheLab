import type { Metadata, Viewport } from "next";
import { Archivo, IBM_Plex_Mono } from "next/font/google";
import "./globals.css";
import { NavBar } from "@/components/NavBar";
import { PwaSetup } from "@/components/PwaSetup";
import { ChatDock } from "@/components/ChatDock";

const ui = Archivo({ subsets: ["latin"], variable: "--font-ui" });
const mono = IBM_Plex_Mono({ subsets: ["latin"], weight: ["400", "500", "600"], variable: "--font-mono" });

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
    <html lang="en" className={`${ui.variable} ${mono.variable}`}>
      <body>
        <PwaSetup />
        <NavBar />
        <div className="shell">
          <main>{children}</main>
          <ChatDock />
        </div>
      </body>
    </html>
  );
}

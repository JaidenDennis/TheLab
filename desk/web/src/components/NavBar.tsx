"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const TABS = [
  { href: "/today", label: "Today" },
  { href: "/chat", label: "Chat" },
  { href: "/journal", label: "Journal" },
];

export function NavBar() {
  const path = usePathname();
  if (path === "/login" || path.startsWith("/auth")) return null;
  return (
    <nav className="topnav">
      {TABS.map((t) => (
        <Link key={t.href} href={t.href} className={path.startsWith(t.href) ? "active" : ""}>
          {t.label}
        </Link>
      ))}
    </nav>
  );
}

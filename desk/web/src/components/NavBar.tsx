"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const TABS = [
  { href: "/today", label: "Today" },
  { href: "/journal", label: "Journal" },
];

export function NavBar() {
  const path = usePathname();
  if (path === "/login" || path.startsWith("/auth")) return null;
  return (
    <nav className="topnav">
      <span className="wordmark">The Desk</span>
      {TABS.map((t) => (
        <Link key={t.href} href={t.href} className={"tab" + (path.startsWith(t.href) ? " active" : "")}>
          {t.label}
        </Link>
      ))}
    </nav>
  );
}

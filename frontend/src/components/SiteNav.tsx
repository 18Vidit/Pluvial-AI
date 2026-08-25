"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "The signal" },
  { href: "/address", label: "Analyse an address" },
  { href: "/board", label: "Houston queue" },
  { href: "/lookup", label: "Look up a street" },
];

export function SiteNav() {
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-40 border-b border-ground-700 bg-ground-900/92 backdrop-blur-sm">
      <nav
        aria-label="Primary"
        className="mx-auto flex h-14 w-full max-w-7xl items-center gap-6 px-5 sm:px-8"
      >
        <Link href="/" className="group flex items-center gap-2.5 shrink-0">
          {/* Section-cut mark: surface line over strata — the same idea the
              whole product runs on, at favicon scale. */}
          <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true" className="shrink-0">
            <path d="M1 4.5h18" stroke="var(--bone)" strokeWidth="1.5" strokeLinecap="round" />
            <rect x="1" y="7" width="18" height="3.6" fill="var(--clay-light)" opacity="0.9" />
            <rect x="1" y="11.4" width="18" height="3.6" fill="var(--clay)" opacity="0.8" />
            <rect x="1" y="15.8" width="18" height="2.6" fill="var(--clay-deep)" opacity="0.7" />
            <circle cx="13" cy="13.2" r="1.7" fill="var(--ground-900)" stroke="var(--oxide-bright)" strokeWidth="1.2" />
          </svg>
          <span className="display text-[17px] tracking-tight text-bone">Pluvial-AI</span>
        </Link>

        {/* Scrolls rather than wraps: below ~360px three labels cannot fit on
            one row, and a wrapped nav breaks the header height. */}
        <div className="ml-auto flex items-center gap-1 overflow-x-auto no-scrollbar">
          {LINKS.map((link) => {
            const active =
              link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
            return (
              <Link
                key={link.href}
                href={link.href}
                aria-current={active ? "page" : undefined}
                className={`whitespace-nowrap rounded px-2.5 sm:px-3 py-2 text-[13px] sm:text-sm transition-colors duration-200 ${
                  active
                    ? "text-bone bg-ground-800"
                    : "text-bone-dim hover:text-bone hover:bg-ground-850"
                }`}
              >
                {link.label}
              </Link>
            );
          })}
        </div>
      </nav>
    </header>
  );
}

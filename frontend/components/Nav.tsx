"use client";

import clsx from "clsx";
import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Overview" },
  { href: "/opportunities", label: "Opportunities" },
  { href: "/gaps", label: "Content gaps" },
  { href: "/trends", label: "Trends" },
  { href: "/github", label: "GitHub" },
  { href: "/hackernews", label: "Hacker News" },
  { href: "/twitch", label: "Twitch" },
  { href: "/recommendations", label: "Recommendations" },
  { href: "/saved", label: "Saved ideas" },
  { href: "/settings", label: "Settings" },
];

export function Nav() {
  const path = usePathname();
  return (
    <nav aria-label="Main" className="flex flex-wrap gap-1">
      {LINKS.map((l) => {
        const active = l.href === "/" ? path === "/" : path.startsWith(l.href);
        return (
          <Link
            key={l.href}
            href={l.href}
            aria-current={active ? "page" : undefined}
            className={clsx(
              "rounded-md px-3 py-1.5 text-sm",
              active ? "bg-raised font-semibold text-ink" : "text-ink-2 hover:bg-raised hover:text-ink",
            )}
          >
            {l.label}
          </Link>
        );
      })}
    </nav>
  );
}

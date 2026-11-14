"use client";

import { useState } from "react";

/** Dark is the default; the choice is kept in a cookie so the server renders it. */
export function ThemeToggle({ initial }: { initial: "dark" | "light" }) {
  const [theme, setTheme] = useState(initial);
  function toggle() {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.classList.toggle("dark", next === "dark");
    document.cookie = `theme=${next}; path=/; max-age=31536000; samesite=lax`;
  }
  return (
    <button
      type="button"
      onClick={toggle}
      className="rounded-md border border-line px-3 py-1.5 text-sm text-ink-2 hover:bg-raised"
      aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
    >
      {theme === "dark" ? "Light theme" : "Dark theme"}
    </button>
  );
}

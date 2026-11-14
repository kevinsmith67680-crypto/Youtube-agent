import type { Config } from "tailwindcss";

// Colors are CSS variables (app/globals.css) so the dark theme (default) and the light
// theme are each selected steps, not an automatic inversion.
const v = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

const config: Config = {
  darkMode: "class",
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        page: v("page"),
        surface: v("surface"),
        raised: v("raised"),
        ink: v("ink"),
        "ink-2": v("ink-2"),
        muted: v("muted"),
        line: v("line"),
        accent: v("accent"),
        action: v("action"),
        "series-1": v("series-1"),
        "series-2": v("series-2"),
        "series-3": v("series-3"),
        good: v("good"),
        warning: v("warning"),
        serious: v("serious"),
        critical: v("critical"),
        synthetic: v("synthetic"),
      },
      fontFamily: {
        sans: ["system-ui", "-apple-system", '"Segoe UI"', "sans-serif"],
      },
    },
  },
  plugins: [],
};

export default config;

import type { Config } from "tailwindcss";

/**
 * RESOLVE design tokens — sampled from the approved "Color Me Coral" reference palette.
 * These are the ONLY brand colors used across the landing page and the agent workspace.
 * Nothing here is a component-library default.
 */
const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        surface: "#FBFBF9",
        "primary-rose": "#BC6166",
        "primary-pink": "#CE8491",
        "neutral-sand": "#D4C4B3",
        "neutral-blush": "#E7B9AA",
        "accent-terracotta": "#D27D54",
        "text-charcoal": "#32312D",
      },
      fontFamily: {
        display: ["var(--font-display)", "Georgia", "serif"],
        body: ["var(--font-body)", "system-ui", "sans-serif"],
      },
      borderRadius: {
        xl2: "1.25rem",
      },
      boxShadow: {
        soft: "0 8px 30px -10px rgba(50, 49, 45, 0.15)",
        card: "0 4px 18px -6px rgba(50, 49, 45, 0.12)",
      },
    },
  },
  plugins: [],
};

export default config;

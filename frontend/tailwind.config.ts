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
        // Semantic status colors (info / verified / pending / failure). `-soft` variants
        // are the tinted backgrounds paired with the DEFAULT foregrounds.
        status: {
          info: { DEFAULT: "#0369A1", soft: "#E0F2FE" },
          success: { DEFAULT: "#047857", soft: "#D1FAE5" },
          pending: { DEFAULT: "#B45309", soft: "#FEF3C7" },
          failure: { DEFAULT: "#B91C1C", soft: "#FEE2E2" },
        },
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
      keyframes: {
        "fade-in": {
          from: { opacity: "0", transform: "translateY(4px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        "fade-in": "fade-in 0.25s ease-out",
      },
    },
  },
  plugins: [],
};

export default config;

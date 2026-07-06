import type { Config } from "tailwindcss";
import colors from "tailwindcss/colors";

// ─────────────────────────────────────────────────────────────────────────────
// Dark mode: per-utility FOLDING palette swap.
//
// The app's ~4700 hardcoded neutral utilities (bg-white, text-gray-900, …) are
// re-pointed at CSS variables PER UTILITY TYPE, so background, border and text
// each get their own ramp with a FOLDING (non-mirror) dark mapping:
//   - surfaces: white + gray-50..400 flip dark; gray-500..950/black UNCHANGED
//     (deliberate dark-in-light surfaces — brand buttons, terminals, scrims,
//     the login panel — stay dark on both themes, like the agent avatar).
//   - text: gray-400..900 fold to light; gray-50..300 + white/black UNCHANGED
//     (light text on always-dark elements keeps working, text-white stays white).
//   - lines: gray-50..400 flip to dark border tones; the rest unchanged.
//   - accent chip tones (…-50/100/200 surfaces+borders, …-600/700/800 text)
//     flip via the same mechanism.
// :root in app/globals.css holds the EXACT stock Tailwind values, so with the
// toggle off the compiled colors are byte-identical to before; the `.dark`
// values live next to them. Every touched family is spread from the stock
// palette first, so no color step can disappear regardless of theme-merge
// semantics.
// ─────────────────────────────────────────────────────────────────────────────
const v = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

const ACCENTS = [
  "blue", "red", "amber", "green", "emerald", "violet", "purple", "yellow",
  "orange", "indigo", "sky", "teal", "pink", "rose", "cyan", "slate",
] as const;

const pal = (fam: string) => (colors as Record<string, any>)[fam];

// Surfaces (backgroundColor + gradient stops): white + gray light-end + accent chips.
const surfaceOverrides = {
  white: v("sfc-white"),
  gray: {
    ...pal("gray"),
    50: v("sfc-gray-50"), 100: v("sfc-gray-100"), 200: v("sfc-gray-200"),
    300: v("sfc-gray-300"), 400: v("sfc-gray-400"),
  },
  ...Object.fromEntries(ACCENTS.map((f) => [f, {
    ...pal(f),
    50: v(`acc-${f}-50`), 100: v(`acc-${f}-100`), 200: v(`acc-${f}-200`),
  }])),
};

// Lines (border/divide/ring): gray light-end + accent 200 borders.
const lineGray = {
  ...pal("gray"),
  50: v("lin-gray-50"), 100: v("lin-gray-100"), 200: v("lin-gray-200"),
  300: v("lin-gray-300"), 400: v("lin-gray-400"),
};
const borderOverrides = {
  gray: lineGray,
  ...Object.fromEntries(ACCENTS.map((f) => [f, { ...pal(f), 200: v(`acc-${f}-200`) }])),
};

// Text (text/placeholder): gray dark-end folds to light + accent 600/700/800.
// white/black are deliberately NOT overridden here.
const textOverrides = {
  gray: {
    ...pal("gray"),
    400: v("txt-gray-400"), 500: v("txt-gray-500"), 600: v("txt-gray-600"),
    700: v("txt-gray-700"), 800: v("txt-gray-800"), 900: v("txt-gray-900"),
  },
  ...Object.fromEntries(ACCENTS.map((f) => [f, {
    ...pal(f),
    600: v(`acc-${f}-600t`), 700: v(`acc-${f}-700t`), 800: v(`acc-${f}-800t`),
  }])),
};

const config: Config = {
  darkMode: "class",
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
      },
      backgroundColor: surfaceOverrides,
      gradientColorStops: surfaceOverrides,
      borderColor: borderOverrides,
      divideColor: { gray: lineGray },
      ringColor: { gray: lineGray },
      textColor: textOverrides,
      placeholderColor: {
        gray: { ...pal("gray"), 400: v("txt-gray-400"), 500: v("txt-gray-500") },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
    },
  },
  plugins: [],
};
export default config;

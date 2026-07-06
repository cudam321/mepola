/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    // vintage OS: 2px everywhere — blocky, no soft cards
    borderRadius: {
      none: "0",
      sm: "2px",
      DEFAULT: "2px",
      md: "2px",
      lg: "2px",
      xl: "2px",
      "2xl": "2px",
      "3xl": "2px",
      full: "2px",
    },
    // pixel-font scale: Departure Mono is chunky — it reads best ~12-16px
    // NOTE: no `base` key here on purpose — `base` is a COLOR (below), and a `text-base`
    // font-size would collide with it, so `hover:text-base` (dark text on a bright button) would
    // also bump the font-size and make the button GROW on hover. Use text-[14px] for that size.
    fontSize: {
      xs: ["12px", { lineHeight: "1.35" }],
      sm: ["13px", { lineHeight: "1.4" }],
      md: ["14px", { lineHeight: "1.45" }],
      lg: ["16px", { lineHeight: "1.45" }],
      xl: ["20px", { lineHeight: "1.35" }],
      "2xl": ["24px", { lineHeight: "1.3" }],
    },
    extend: {
      colors: {
        base: "#060A05", // crt-black — near-black, faint green cast
        panel: "rgba(9,15,6,0.82)", // panel surface (solid, no blur)
        panel2: "#0C1409", // raised surface (axis pointer, thead)
        edge: "#36480E", // panel border — rgba(147,192,31,0.34) over base, flattened
        grid: "#1D2709", // hairline — rgba(147,192,31,0.16) over base, flattened
        win: "#3DDC84", // P&L green — deliberately NOT the lime chrome
        loss: "#FF5147", // P&L red — hot, reads instantly
        live: "#CBF14E", // phosphor-bright — live / links / active state
        tail: "#CBF14E", // accent (the tail) — phosphor-bright
        hot: "#F6FFE1", // RESERVED incandescent — ≥10x winners only
        ink: "#A6D63C", // primary body text & numbers
        muted: "#6F8E38", // secondary labels
      },
      fontFamily: {
        // one typeface: the whole system is the Departure Mono pixel face
        sans: ["DepartureMono", "Pixelify Sans", "ui-monospace", "Menlo", "monospace"],
        mono: ["DepartureMono", "Pixelify Sans", "ui-monospace", "Menlo", "monospace"],
        plex: ["DepartureMono", "Pixelify Sans", "ui-monospace", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};

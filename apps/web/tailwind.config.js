/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        obsidian: {
          canvas: "#0A0A0B",
          surface: "#111113",
          elevated: "#18181B",
          hover: "#202024",
        },
        ink: {
          primary: "#F4F4F5",
          secondary: "#A1A1AA",
          muted: "#71717A",
        },
        diag: {
          upstream: "#38BDF8",
          downstream: "#F59E0B",
          conflict: "#F43F5E",
          approved: "#34D399",
          quarantine: "#F59E0B",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "-apple-system",
          "BlinkMacSystemFont",
          "SF Pro Text",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "SF Mono",
          "Cascadia Code",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "monospace",
        ],
      },
    },
  },
  plugins: [],
};

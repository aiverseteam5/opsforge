/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#0b0f14",
        panel: "#121821",
        edge: "#1f2935",
        muted: "#8b97a7",
      },
    },
  },
  plugins: [],
};

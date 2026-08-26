import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Trading Desk",
    short_name: "Desk",
    description: "Journal + buddy for discretionary NQ trading",
    start_url: "/today",
    display: "standalone",
    background_color: "#101215",
    theme_color: "#101215",
    icons: [
      { src: "/icon.svg", sizes: "any", type: "image/svg+xml" },
      { src: "/apple-icon.png", sizes: "180x180", type: "image/png" },
    ],
  };
}

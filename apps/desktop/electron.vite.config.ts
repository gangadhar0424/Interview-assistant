import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import { defineConfig, externalizeDepsPlugin } from "electron-vite";

// Vite's dev server injects <style> tags for CSS. The packaged build ships a
// static stylesheet and keeps the strict `style-src 'self'` from index.html;
// only the dev server (`apply: "serve"`) gets inline styles.
const devOnlyInlineStyles = {
  name: "dev-only-inline-styles",
  apply: "serve" as const,
  transformIndexHtml: (html: string) =>
    html.replace("style-src 'self';", "style-src 'self' 'unsafe-inline';"),
};

export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()],
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
  },
  renderer: {
    css: {
      postcss: { plugins: [] },
    },
    resolve: {
      alias: {
        "@renderer": resolve("src/renderer/src"),
      },
    },
    plugins: [react(), tailwindcss(), devOnlyInlineStyles],
  },
});

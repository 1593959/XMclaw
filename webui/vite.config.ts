import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// 构建产物直接落到 daemon 包内（提交进 git，最终用户零 Node）。
// base "./" 让同一份产物既能挂 /ui-next/（并存期）也能挂 /ui/（M3 切换后）。
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../xmclaw/daemon/webui_dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8766",
      "/agent": { target: "ws://127.0.0.1:8766", ws: true },
    },
  },
});

import { defineConfig } from "astro/config";

// site/ 平铺 HTML（index.html / {board}-{date}.html / topic-{date}-{key}.html / archive.html）
// format:"file" 保证 file:// 直开与相对链接可用，与旧 Jinja 输出的形态一致。
// inlineStylesheets:"always"——CSS 全部内联进页面：外链 /_astro/*.css 是绝对路径，
// file:// 直开与子路径托管（未来 GitHub Pages）都会 404（2026-07-04 实际踩坑）。
export default defineConfig({
  output: "static",
  outDir: "../site",
  build: { format: "file", inlineStylesheets: "always" },
});

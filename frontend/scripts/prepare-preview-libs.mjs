import { copyFile, mkdir } from "node:fs/promises";
const root = new URL("../", import.meta.url);
const output = new URL("public/preview-libs/", root);
await mkdir(output, { recursive: true });
for (const [source, name] of [
  ["echarts/dist/echarts.min.js", "echarts-6.0.0.min.js"],
  ["chart.js/dist/chart.umd.min.js", "chartjs-4.5.1.min.js"],
  ["echarts/LICENSE", "echarts-LICENSE.txt"],
  ["chart.js/LICENSE.md", "chartjs-LICENSE.txt"],
]) await copyFile(new URL(`node_modules/${source}`, root), new URL(name, output));

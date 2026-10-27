import { describe, expect, it } from "vitest";

import { artifactRendererFor } from "./registry";

describe("artifactRendererFor", () => {
  it.each([
    ["page.html", "text/html", "html"],
    ["page.HTM", "application/octet-stream", "html"],
    ["page.xhtml", "application/xhtml+xml", "html"],
    ["page", "text/html; charset=utf-8", "html"],
    ["report.md", "text/markdown", "markdown"],
    ["data.json", "application/json", "json"],
    ["config.yaml", "application/yaml", "yaml"],
    ["table.csv", "text/csv", "csv"],
    ["photo.png", "image/png", "image"],
    ["deck.pdf", "application/pdf", "pdf"],
    ["main.py", "text/x-python", "code"],
  ])("maps %s to a passive renderer", (name, mime, expected) => {
    expect(artifactRendererFor(name, mime)).toBe(expected);
  });

  it.each([

    ["vector.svg", "image/svg+xml"],
    ["macro.js", "text/javascript"],
    ["sheet.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
  ])("forces unsupported active or office content to download", (name, mime) => {
    expect(artifactRendererFor(name, mime)).toBe("download");
  });
});

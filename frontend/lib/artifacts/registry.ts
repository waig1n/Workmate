export type ArtifactRenderer =
  | "html"
  | "markdown"
  | "text"
  | "code"
  | "json"
  | "yaml"
  | "csv"
  | "image"
  | "pdf"
  | "download";

const CODE_EXTENSIONS = new Set([
  "py", "ts", "tsx", "js", "jsx", "java", "go", "rs", "c", "cc", "cpp",
  "h", "hpp", "sh", "bash", "zsh", "sql", "toml", "ini", "conf", "xml",
]);
const TEXT_EXTENSIONS = new Set(["txt", "log"]);
const ACTIVE_EXTENSIONS = new Set(["svg", "js", "mjs", "cjs"]);

function extension(name: string): string {
  const clean = name.toLowerCase().split(/[?#]/, 1)[0];
  const index = clean.lastIndexOf(".");
  return index < 0 ? "" : clean.slice(index + 1);
}

export function artifactRendererFor(name: string, mimeType: string): ArtifactRenderer {
  const ext = extension(name);
  const mime = mimeType.toLowerCase().split(";", 1)[0].trim();
  if (
    ACTIVE_EXTENSIONS.has(ext)
    || mime === "image/svg+xml"
    || mime.includes("javascript")
  ) return "download";
  if (["html", "htm", "xhtml"].includes(ext) || ["text/html", "application/xhtml+xml"].includes(mime)) return "html";
  if (ext === "md" || ext === "markdown" || mime === "text/markdown") return "markdown";
  if (ext === "json" || mime === "application/json") return "json";
  if (["yaml", "yml"].includes(ext) || ["application/yaml", "text/yaml"].includes(mime)) return "yaml";
  if (ext === "csv" || mime === "text/csv") return "csv";
  if (mime.startsWith("image/") && mime !== "image/svg+xml") return "image";
  if (ext === "pdf" || mime === "application/pdf") return "pdf";
  if (CODE_EXTENSIONS.has(ext)) return "code";
  if (TEXT_EXTENSIONS.has(ext) || mime.startsWith("text/plain")) return "text";
  return "download";
}

export function canRenderAsText(renderer: ArtifactRenderer): boolean {
  return ["html", "markdown", "text", "code", "json", "yaml", "csv"].includes(renderer);
}

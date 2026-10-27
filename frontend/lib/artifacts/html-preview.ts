import { parse, parseFragment, serialize, type DefaultTreeAdapterTypes as Tree } from "parse5";

// Scripts run only in an opaque-origin sandbox. CSP limits resource requests;
// it cannot prevent arbitrary scripts from navigating their own frame.
export const INTERACTIVE_HTML_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'";
const LIBRARIES = {
  echarts: "/preview-libs/echarts-6.0.0.min.js",
  chartjs: "/preview-libs/chartjs-4.5.1.min.js",
} as const;
type Library = keyof typeof LIBRARIES;
type Loader = (library: Library, signal?: AbortSignal) => Promise<string>;

const loadLibrary: Loader = async (library, signal) => {
  const response = await fetch(LIBRARIES[library], { signal, cache: "force-cache", credentials: "omit" });
  if (!response.ok) throw new Error("图表库加载失败，请重试或下载文件");
  return response.text();
};

function chartLibrary(src: string): Library | undefined {
  // Aliases only, never URLs to fetch or proxy. All versions use pinned local builds.
  const path = src.split(/[?#]/, 1)[0].toLowerCase();
  if (path === LIBRARIES.echarts) return "echarts";
  if (path === LIBRARIES.chartjs) return "chartjs";
  if (/(?:^|\/)echarts(?:@[^/]+)?(?:\/dist\/echarts(?:\.min)?\.js|(?:\.min)?\.js)?$/.test(path)) return "echarts";
  if (/(?:^|\/)chart(?:\.js(?:@[^/]+)?(?:\/dist\/chart(?:\.umd)?(?:\.min)?\.js)?|(?:\.umd)?(?:\.min)?\.js)$/.test(path)) return "chartjs";
  return undefined;
}

export async function dynamicHtmlDocument(source: string, signal?: AbortSignal, load: Loader = loadLibrary) {
  signal?.throwIfAborted();
  // Pure parser: no DOM mounting, script execution or resource loading.
  const doc = parse(source);
  const libraries = new Set<Library>();
  let blockedScripts = 0;
  const pending: Tree.ParentNode[] = [doc];
  const root = doc.childNodes.find(node => "tagName" in node && node.tagName === "html") as Tree.Element;
  const head = root.childNodes.find(node => "tagName" in node && node.tagName === "head") as Tree.Element;
  while (pending.length) {
    const parent = pending.pop()!;
    parent.childNodes = parent.childNodes.filter(node => {
      if (!("tagName" in node)) return true;
      const tag = node.tagName;
      const attr = (name: string) => node.attrs.find(item => item.name === name)?.value;
      if (["base", "iframe", "frame", "frameset", "object", "embed", "link"].includes(tag)
          || (tag === "meta" && attr("http-equiv"))) return false;
      if (tag === "script" && attr("src") !== undefined) {
        const library = chartLibrary(attr("src")!);
        if (library) libraries.add(library);
        else blockedScripts += 1;
        return false;
      }
      if (tag === "script") {
        const code = node.childNodes.filter(n => n.nodeName === "#text").map(n => (n as Tree.TextNode).value).join("");
        if (/\becharts\s*\./.test(code)) libraries.add("echarts");
        if (/\b(?:new\s+Chart\b|Chart\s*\.)/.test(code)) libraries.add("chartjs");
      }
      // Keep chart controls but remove declarative navigation. JS self-navigation
      // remains a documented limitation of browser sandboxing.
      if (tag === "form") { node.tagName = "div"; node.nodeName = "div"; }
      node.attrs = node.attrs.filter(item => !["action", "formaction", "target", "download", "ping"].includes(item.name)
        && !(["a", "area"].includes(tag) && item.name === "href"));
      pending.push(node);
      if ("content" in node) pending.push((node as Tree.Template).content);
      return true;
    });
  }
  const scripts = await Promise.all([...libraries].map(async library => {
    const code = await load(library, signal);
    return `<script data-preview-library="${library}">${code.replace(/<\/script/gi, "<\\/script")}</script>`;
  }));
  signal?.throwIfAborted();
  const trusted = parseFragment(`<meta http-equiv="Content-Security-Policy" content="${INTERACTIVE_HTML_CSP}"><meta charset="utf-8"><meta name="referrer" content="no-referrer">${scripts.join("")}`);
  // The parser creates a head for ordinary documents. A frameset document may
  // have lost its body during filtering; head still holds the enforced policy.
  if (!head) throw new Error("HTML 文档结构无效，请查看源码或下载文件");
  for (const node of trusted.childNodes) node.parentNode = head;
  head.childNodes.unshift(...trusted.childNodes);
  return { document: serialize(doc), blockedScripts };
}

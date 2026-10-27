import { describe, expect, it, vi } from "vitest";
import { dynamicHtmlDocument } from "./html-preview";

describe("isolated interactive HTML", () => {
  it("preserves scripts, SVG, chart controls and full document attributes", async () => {
    const result = await dynamicHtmlDocument('<html lang="zh"><body class="report" style="padding:20px"><canvas id="chart"></canvas><button onclick="draw()">更新</button><svg><path d="M0 0"/></svg><script>function draw(){document.body.dataset.drawn="yes"}</script></body></html>');
    const doc = new DOMParser().parseFromString(result.document, "text/html");
    expect(doc.body.className).toBe("report");
    expect(doc.body.style.padding).toBe("20px");
    expect(doc.querySelector("canvas")).not.toBeNull();
    expect(doc.querySelector("svg")).not.toBeNull();
    expect(doc.querySelector("button")?.getAttribute("onclick")).toBe("draw()");
    expect(doc.querySelector("script")?.textContent).toContain("function draw");
    expect(doc.head.firstElementChild?.getAttribute("content")).toContain("script-src 'unsafe-inline'");
    expect(doc.head.firstElementChild?.getAttribute("content")).toContain("connect-src 'none'");
  });

  it("replaces chart CDN scripts with fixed local libraries without fetching user URLs", async () => {
    const load = vi.fn(async (name: string) => `window.${name}=true; // </script><img src=x>`);
    const result = await dynamicHtmlDocument('<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script><script src="https://cdn.jsdelivr.net/npm/chart.js"></script><script src="https://bad.test/unknown.js"></script><script>echarts.init(document.body)</script>', undefined, load);
    expect(new Set(load.mock.calls.map(call => call[0]))).toEqual(new Set(["echarts", "chartjs"]));
    expect(result.blockedScripts).toBe(1);
    const doc = new DOMParser().parseFromString(result.document, "text/html");
    expect(doc.querySelector("script[src],img")).toBeNull();
    expect(doc.querySelectorAll("script")).toHaveLength(3);
    expect(doc.querySelector("script")?.textContent).toContain("<\\/script>");
  });

  it("removes declarative navigation while documenting arbitrary JS self-navigation", async () => {
    const result = await dynamicHtmlDocument('<meta http-equiv="refresh" content="0;url=https://bad.test"><base href="https://bad.test"><iframe srcdoc="bad"></iframe><form><input><button>send</button></form><a href="https://bad.test" ping="https://bad.test">Link</a><script>location.href="https://bad.test"</script>');
    const doc = new DOMParser().parseFromString(result.document, "text/html");
    expect(doc.querySelector('meta[http-equiv="refresh"],base,iframe,form,[href],[ping]')).toBeNull();
    expect(doc.querySelector("input")).not.toBeNull();
    expect(doc.querySelector("script")?.textContent).toContain("location.href");
  });

  it("reports library failures instead of silently showing a blank chart", async () => {
    await expect(dynamicHtmlDocument('<script>echarts.init(x)</script>', undefined, async () => { throw new Error("图表库加载失败"); })).rejects.toThrow("图表库加载失败");
  });

  it("aborts a stale library preparation", async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(dynamicHtmlDocument('<h1>old</h1>', controller.signal)).rejects.toThrow();
  });
});

it.each([
  ["https://cdnjs.cloudflare.com/ajax/libs/echarts/5.5.0/echarts.min.js", "echarts"],
  ["https://unpkg.com/echarts@5", "echarts"],
  ["/preview-libs/echarts-6.0.0.min.js", "echarts"],
  ["/preview-libs/chartjs-4.5.1.min.js", "chartjs"],
  ["https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js", "chartjs"],
])("maps supported library alias %s to a fixed local build", async (url, library) => {
  const load = vi.fn(async () => "/* local fixture */");
  const result = await dynamicHtmlDocument(`<script src="${url}"></script>`, undefined, load);
  expect(load).toHaveBeenCalledWith(library, undefined);
  expect(result.blockedScripts).toBe(0);
});

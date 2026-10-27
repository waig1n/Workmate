"use client";

import { useEffect, useState } from "react";
import { resourceDrawerWidth } from "@/components/ui/ResourceCard";
import { Dialog } from "@/components/ui/Dialog";
import { FormError, headerRecord, importConnection, probeError, validateEndpoint } from "./connectionForm";
import { api } from "@/lib/api";
import type { InstalledMCP, MCPCard } from "@/lib/types";

type Configuration = {
  source: "custom" | "catalog"; name: string; display_name?: string; description?: string;
  url?: string; timeout?: number; is_stateful: boolean; header_names: string[];
  configured_fields: string[]; inputs_schema?: MCPCard["inputs_schema"];
};
type Probe = { ok: boolean; tools: { name: string }[]; storage_node_id?: string; error_code?: string };
const control = "product-control mt-1 w-full px-3 py-2";

export function MCPConfiguration({ id, scopeId, onClose, onSaved }: {
  id?: string; scopeId: string; onClose: () => void; onSaved: (item: InstalledMCP, options?: { keepOpen?: boolean }) => void;
}) {
  const [configuration, setConfiguration] = useState<Configuration | null>(null);
  const [generatedName] = useState(() => `mcp-${Array.from(crypto.getRandomValues(new Uint8Array(4)), (value) => value.toString(16).padStart(2, "0")).join("")}`);
  const [dirty, setDirty] = useState(false);
  const [savedNotice, setSavedNotice] = useState(false);
  const [probeFailed, setProbeFailed] = useState(false);
  const [name, setName] = useState(id ? "" : generatedName);
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [url, setUrl] = useState("");
  const [timeout, setTimeoutValue] = useState(30);
  const [stateful, setStateful] = useState(false);
  const [auth, setAuth] = useState(id ? "preserve" : "none");
  const [token, setToken] = useState("");
  const [apiKeyName, setApiKeyName] = useState("X-API-Key");
  const [mode, setMode] = useState("form");
  const [json, setJson] = useState("");
  const [headers, setHeaders] = useState<{ name: string; value: string }[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(Boolean(id));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [probe, setProbe] = useState<Probe | null>(null);
  useEffect(() => {
    if (!id) return;
    let active = true;
    api.get<Configuration>(`/mcps/${id}/configuration`).then((result) => {
      if (!active) return;
      setConfiguration(result); setName(result.name); setDisplayName(result.display_name ?? "");
      setDescription(result.description ?? ""); setUrl(result.url ?? "");
      setTimeoutValue(result.timeout ?? 30); setStateful(result.is_stateful);
    }).catch(() => { if (active) setError("连接配置加载失败，请关闭后重试。"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [id]);
  const custom = !id || configuration?.source === "custom";
  function payload(testing = false) {
    const endpoint = validateEndpoint(url);
    const connectionName = name.trim() || (testing ? "mcp-probe" : generatedName);
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(connectionName)) throw new FormError(probeError("invalid_name"));
    if (!Number.isFinite(timeout) || timeout < 1 || timeout > 120) throw new FormError(probeError("invalid_timeout"));
    if (["bearer", "apiKey"].includes(auth) && !token.trim()) throw new FormError("请填写认证凭据。");
    const rows = auth === "bearer" ? [{ name: "Authorization", value: `Bearer ${token.trim()}` }]
      : auth === "apiKey" ? [{ name: apiKeyName, value: token }] : auth === "custom" ? headers : [];
    return { name: connectionName, display_name: displayName.trim(), description: description.trim(), url: endpoint, timeout, is_stateful: stateful,
      ...(auth === "preserve" ? {} : { headers: headerRecord(rows) }) };
  }
  function importJson() {
    try {
      const imported = importConnection(json);
      setUrl(imported.url); if (imported.name) setName(imported.name);
      setHeaders(imported.headers); setAuth(imported.headers.length ? "custom" : "none");
      setToken(""); setJson(""); setMode("form"); setError(""); setProbe(null); setDirty(true); setProbeFailed(false);
    } catch (reason) { setError(reason instanceof FormError ? reason.message : "JSON 导入失败，请检查配置格式。"); }
  }
  async function runProbe(path: string, body: object = {}) {
    const result = await api.post<Probe>(path, body);
    if (result.ok) setProbe(result);
    else { setProbeFailed(true); setError(probeError(result.error_code)); }
  }
  async function save(andTest = false) {
    setError(""); setProbe(null); setProbeFailed(false); setSavedNotice(false);
    let body;
    try { body = custom ? payload() : { name: name.trim(), values }; }
    catch (reason) { setError(reason instanceof FormError ? reason.message : probeError("invalid_configuration")); return; }
    setBusy(true);
    let saved = false;
    try {
      const item = id
        ? await api.patch<InstalledMCP>(`/mcps/${id}${custom ? "/custom" : ""}`, body)
        : await api.post<InstalledMCP>("/mcps/custom", { ...body, scope_id: scopeId });
      saved = true;
      if (!andTest) { onSaved(item); onClose(); return; }
      // Keep the new saved baseline, without retaining submitted credentials.
      setDirty(false); setSavedNotice(true); setAuth("preserve"); setName(item.name);
      setToken(""); setHeaders([]); setValues({});
      if (custom && configuration) {
        const submitted = body as ReturnType<typeof payload>;
        setConfiguration({ ...configuration, header_names: submitted.headers
          ? Object.keys(submitted.headers) : configuration.header_names });
      }
      onSaved(item, { keepOpen: true });
      await runProbe(`/mcps/${id}/probe`);
    } catch (reason) {
      if (saved) setProbeFailed(true);
      setError(probeError((reason as { code?: string })?.code));
    } finally { setBusy(false); }
  }
  async function testConnection() {
    if (id && dirty) { await save(true); return; }
    setError(""); setProbe(null); setProbeFailed(false);
    let body = {};
    if (!id) {
      try {
        const draft = payload(true);
        body = { scope_id: scopeId, configuration: { name: draft.name, is_stateful: draft.is_stateful, mcp_config: { type: "http_mcp", url: draft.url, headers: draft.headers, timeout: draft.timeout } } };
      } catch (reason) { setError(reason instanceof FormError ? reason.message : probeError("invalid_configuration")); return; }
    }
    setBusy(true);
    try { await runProbe(id ? `/mcps/${id}/probe` : "/mcps/probe", body); }
    catch (reason) { setProbeFailed(true); setError(probeError((reason as { code?: string })?.code)); }
    finally { setBusy(false); }
  }
  return <Dialog open side width={resourceDrawerWidth} onClose={() => { if (!busy) onClose(); }} title={id ? "编辑 MCP 连接" : "添加自定义连接"}>
    <form noValidate className="space-y-5 text-sm" onChange={() => { if (mode === "json") return; setProbe(null); setProbeFailed(false); setDirty(true); setSavedNotice(false); }} onSubmit={(event) => { event.preventDefault(); void save(); }}>
      {savedNotice && <p role="status" className="rounded-xl bg-[var(--surface-subtle)] p-3">配置已保存。{probeFailed ? "连接测试失败，可重试测试。" : ""}</p>}
      {error && <p role="alert" className="rounded-xl bg-red-50 p-3 text-red-700 dark:bg-red-950 dark:text-red-300">{error}</p>}
      <fieldset disabled={busy} className="min-w-0 space-y-5">
      {loading ? <p role="status">正在加载配置…</p> : (!id || configuration) && <>
        <p className="text-xs text-[var(--ink-soft)]">凭据仅用于受管连接，不向使用者公开。保存后须设置共享范围，Agent 才能选用。</p>
        {custom && <div className="flex gap-2 rounded-xl bg-[var(--surface-subtle)] p-1" role="group" aria-label="配置输入方式">{[["form", "填写表单"], ["json", "导入 JSON"]].map(([value, label]) => <button key={value} type="button" aria-pressed={mode === value} className={`flex-1 rounded-lg px-3 py-2 ${mode === value ? "bg-[var(--surface-raised)] shadow-sm" : ""}`} onClick={() => { setMode(value); setError(""); }}>{label}</button>)}</div>}
        {custom && mode === "json" ? <div className="space-y-3">
          <label className="block">JSON 配置<textarea className={`${control} min-h-48 font-mono text-xs`} value={json} onChange={(event) => setJson(event.target.value)} autoComplete="off" spellCheck={false} placeholder={'{"mcpServers":{"reports":{"url":"https://example.com/mcp"}}}'} /></label>
          <p className="text-xs text-[var(--ink-soft)]">支持 mcpServers、servers 或单个连接对象。一次只导入一个 HTTP/SSE 连接，不支持命令、OAuth 或变量占位符。导入只填充表单，不发起连接。</p>
          <button type="button" className="rounded-lg border px-4 py-2" onClick={importJson}>填入表单</button>
        </div> : <>
        {custom && <section className="space-y-4">
          <h3 className="font-medium">基本信息</h3>
          <label className="block">显示名称<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} className={control} placeholder="例如：财务报表服务" /></label>
          <label className="block">说明<textarea value={description} onChange={(event) => setDescription(event.target.value)} className={`${control} min-h-20`} placeholder="介绍连接用途，方便成员选用" /></label>
        </section>}
        <label className="block">连接名称<input value={name} onChange={(event) => setName(event.target.value)} className={control} placeholder={generatedName} /></label>
        <p className="text-xs text-[var(--ink-soft)]">技术标识，支持英文字母、数字、下划线或短横线。{!id && "留空时自动生成，可自行修改。"}</p>
        {custom ? <>
          <label className="block">服务地址<input type="url" value={url} onChange={(event) => setUrl(event.target.value)} className={control} placeholder="https://example.com/mcp" /></label>
          <p className="text-xs leading-5 text-[var(--ink-soft)]">连接由服务器发起。即使服务与平台在同一台机器，也须使用执行容器可访问的地址；仅监听 127.0.0.1 的服务不可达。地址不可包含账号、查询参数或片段。</p>
          <label className="block">认证方式<select className={control} value={auth} onChange={(event) => { setAuth(event.target.value); setToken(""); setHeaders([]); }}>
            {id && <option value="preserve">保留现有认证</option>}<option value="none">无认证</option><option value="bearer">Bearer Token</option><option value="apiKey">API Key 请求头</option><option value="custom">自定义请求头</option>
          </select></label>
          {id && <p className="text-xs text-[var(--ink-soft)]">已配置请求头：{configuration?.header_names.join("、") || "无"}。现有值不回显。选择其他认证方式将替换全部请求头；无认证将清空。</p>}
          {auth === "apiKey" && <label className="block">API Key 请求头名称<input className={control} value={apiKeyName} onChange={(event) => setApiKeyName(event.target.value)} /></label>}
          {["bearer", "apiKey"].includes(auth) && <label className="block">{auth === "bearer" ? "Bearer Token" : "API Key"}<input type="password" autoComplete="new-password" className={control} value={token} onChange={(event) => setToken(event.target.value)} placeholder={auth === "bearer" ? "只填写 Token，无需 Bearer 前缀" : "填写 API Key"} /></label>}
          {auth === "custom" && <div className="space-y-2 rounded-xl border p-3">
            {headers.map((header, index) => <div key={index} className="flex gap-2">
              <input aria-label={`请求头名称 ${index + 1}`} className={`${control} min-w-0`} value={header.name} onChange={(event) => setHeaders(headers.map((item, row) => row === index ? { ...item, name: event.target.value } : item))} placeholder="请求头名称" />
              <input aria-label={`请求头值 ${index + 1}`} type="password" autoComplete="new-password" className={`${control} min-w-0`} value={header.value} onChange={(event) => setHeaders(headers.map((item, row) => row === index ? { ...item, value: event.target.value } : item))} placeholder="值" />
              <button type="button" aria-label={`移除请求头 ${index + 1}`} onClick={() => setHeaders(headers.filter((_, row) => row !== index))}>移除</button>
            </div>)}
            <button type="button" className="rounded-lg border px-3 py-2 font-medium" onClick={() => setHeaders([...headers, { name: "", value: "" }])}>＋ 添加请求头</button>
          </div>}
          <details className="rounded-xl border p-3"><summary className="cursor-pointer font-medium">高级选项</summary><div className="mt-3 space-y-3">
            <label className="block">超时（秒）<input type="number" min="1" max="120" step="any" value={timeout} onChange={(event) => setTimeoutValue(Number(event.target.value))} className={control} /></label>
            <label className="flex gap-2"><input type="checkbox" checked={stateful} onChange={(event) => setStateful(event.target.checked)} />保持会话状态</label>
          </div></details>
        </> : <>
          <p className="text-xs">未填写的字段保留现有配置，已保存的值不会回显。</p>
          {Object.entries(configuration?.inputs_schema?.properties ?? {}).map(([key, spec]) => <label key={key} className="block">{spec.title || key}<input className={control} type={spec.writeOnly || spec.format === "password" ? "password" : "text"} autoComplete="off" value={values[key] ?? ""} onChange={(event) => setValues(Object.fromEntries(Object.entries({ ...values, [key]: event.target.value }).filter(([, value]) => value !== "")))} /><span className="text-xs">{spec.description}</span></label>)}
        </>}
        <p className="text-xs text-[var(--ink-soft)]">测试仅发现工具，不调用工具。{id && (dirty ? "保存并测试会先保存当前修改，再验证新配置。" : "当前测试使用已保存配置。")}</p>
        {!probe && !probeFailed && <p className="text-xs text-[var(--ink-faint)]">{dirty ? "当前修改尚未测试" : "尚未测试"}</p>}
        {probeFailed && <p className="text-sm text-red-600">连接测试失败</p>}
        {probe && <div role="status" className="rounded-xl bg-[var(--surface-subtle)] p-4"><p>连接成功，发现 {probe.tools.length} 个工具。</p>{probe.storage_node_id && <p className="text-xs">测试节点：{probe.storage_node_id}。结果仅代表此节点的连接情况。</p>}<p className="break-words text-xs">{probe.tools.map((tool) => tool.name).join("、")}</p></div>}
        <div className="flex justify-end gap-3"><button type="button" disabled={busy} onClick={() => void testConnection()}>{busy ? "处理中…" : id ? dirty ? "保存并测试" : "测试已保存配置" : "测试连接"}</button><button type="submit" disabled={busy} className="rounded-xl bg-[var(--ink)] px-4 py-2 text-[var(--surface)] disabled:opacity-50">{busy ? "处理中…" : "保存连接"}</button></div>
        </>}
      </>}
      </fieldset>
    </form>
  </Dialog>;
}

export type HeaderField = { name: string; value: string };
const messages: Record<string, string> = {
 blocked_target: "此地址属于受限制的目标。连接由服务器发起，请使用服务器可访问的 MCP 地址。",
 dns_error: "服务器无法解析此域名，请检查地址和 DNS 配置。",
 connection_refused: "目标拒绝连接，请确认 MCP 服务已启动并监听服务器可访问的地址。",
 network_error: "服务器无法连接目标，请检查网络与防火墙。",
 tls_error: "TLS 证书验证失败，请检查服务器证书与信任配置。",
 timeout: "连接测试超时，请检查服务状态和网络。",
 authentication_required: "服务需要认证或凭据无效，请选择认证方式并检查凭据。",
 permission_denied: "没有访问权限，请检查管理权限及远端服务账号权限。",
 endpoint_not_found: "未找到 MCP 端点，请检查完整服务地址。",
 protocol_error: "目标未返回有效的 MCP 协议响应，请检查端点与服务版本。",
 worker_unavailable: "执行节点暂不可用，请稍后重试。",
 busy: "连接测试繁忙，请稍后重试。",
 invalid_name: "连接名称须为 1–128 个英文字母、数字、下划线或短横线。",
 invalid_url: "请输入有效的 HTTP 或 HTTPS 地址，不可包含账号、查询参数或片段。",
 invalid_headers: "认证请求头无效，请检查名称、值和重复字段。",
 invalid_timeout: "超时须为 1–120 秒。",
 invalid_configuration: "连接配置无效，请检查填写内容。",
};
export function probeError(code: unknown): string {
 return typeof code === "string" && Object.hasOwn(messages, code) ? messages[code] : "操作未完成，请检查配置或稍后重试。";
}
export class FormError extends Error {}
export function validateEndpoint(value: string): string {
 let parsed: URL;
 try { parsed = new URL(value.trim()); } catch { throw new FormError(messages.invalid_url); }
 const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
 if (host === "localhost" || host.endsWith(".localhost") || host === "[::1]" || /^127\./.test(host)) {
  throw new FormError("localhost、127.0.0.1 和 ::1 指向执行服务器自身。请填写服务器可访问的 MCP 地址；仅监听本机回环地址的服务无法从其他容器访问。");
 }
 if (!["https:", "http:"].includes(parsed.protocol) || parsed.username || parsed.password || /[?#\s]/.test(value) || value.includes("${")) throw new FormError(messages.invalid_url);
 return value.trim();
}
export function headerRecord(rows: HeaderField[]): Record<string, string> {
 const seen = new Set<string>();
 const reserved = new Set(["host", "content-length", "transfer-encoding", "connection", "upgrade", "proxy-authorization", "proxy-connection"]);
 for (const row of rows) {
  const key = row.name.trim().toLowerCase();
  if (seen.has(key)) throw new FormError("请求头名称重复（不区分大小写），请移除重复字段。");
  if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$/.test(row.name.trim()) || reserved.has(key) || /[^\x20-\x7E]/.test(row.value) || row.value.includes("${")) throw new FormError(messages.invalid_headers);
  seen.add(key);
 }
 return Object.fromEntries(rows.map(row => [row.name.trim(), row.value]));
}
function object(value: unknown): value is Record<string, unknown> { return Boolean(value && typeof value === "object" && !Array.isArray(value)); }
function rejectDuplicateKeys(text: string): void {
 const tokens = text.match(/"(?:\\.|[^"\\])*"|[{}\[\]:,]/g) ?? [];
 const stack: (Set<string> | null)[] = [];
 tokens.forEach((token, index) => {
  if (token === "{") stack.push(new Set());
  else if (token === "[") stack.push(null);
  else if (token === "}" || token === "]") stack.pop();
  else if (token.startsWith('"') && tokens[index + 1] === ":") {
   const keys = stack[stack.length - 1];
   const key = JSON.parse(token) as string;
   if (keys?.has(key)) throw new FormError("JSON 包含重复字段，请移除重复字段后导入。");
   keys?.add(key);
  }
 });
}
export function importConnection(text: string): { name: string; url: string; headers: HeaderField[] } {
 let root: unknown;
 try { root = JSON.parse(text); } catch { throw new FormError("JSON 格式无效，请检查后重试。"); }
 rejectDuplicateKeys(text);
 if (!object(root)) throw new FormError("请提供一个 HTTP MCP 配置对象。");
 let name = "";
 let entry: Record<string, unknown> = root;
 const wrapper = "mcpServers" in root ? "mcpServers" : "servers" in root ? "servers" : null;
 if (wrapper) {
  if (Object.keys(root).length !== 1 || !object(root[wrapper])) throw new FormError("仅支持单个 MCP 连接配置，请移除其他配置字段。");
  const entries = Object.entries(root[wrapper]);
  if (entries.length !== 1 || !object(entries[0][1])) throw new FormError("请仅保留一个要导入的 MCP 连接。");
  name = entries[0][0]; entry = entries[0][1] as Record<string, unknown>;
 }
 if (Object.keys(entry).some(key => !["url", "headers", "type", "name"].includes(key)) || (entry.type !== undefined && (typeof entry.type !== "string" || !["http", "sse"].includes(entry.type)))) throw new FormError("仅支持 HTTP/SSE 地址与请求头；不支持命令、环境变量、OAuth 或其他字段。");
 if (entry.name !== undefined) {
  if (typeof entry.name !== "string") throw new FormError("连接名称格式无效。");
  if (name && name !== entry.name) throw new FormError("配置中的连接名称不一致。");
  name = entry.name;
 }
 if (typeof entry.url !== "string") throw new FormError("配置缺少 HTTP MCP 服务地址。");
 const url = validateEndpoint(entry.url);
 const ssePath = /\/(sse|messages\/)$/.test(new URL(url).pathname);
 if (entry.type === "http" && ssePath) throw new FormError("HTTP 类型与 SSE 地址路径冲突；当前 /sse 或 /messages/ 地址按 SSE 连接，请确认配置类型。");
 if (entry.type === "sse" && !ssePath) throw new FormError("当前 SSE 地址须以 /sse 或 /messages/ 结尾。");
 if (entry.headers !== undefined && !object(entry.headers)) throw new FormError(messages.invalid_headers);
 const headers = Object.entries(entry.headers ?? {}).map(([key, value]) => {
  if (typeof value !== "string") throw new FormError(messages.invalid_headers);
  return { name: key, value };
 });
 headerRecord(headers);
 return { name, url, headers };
}

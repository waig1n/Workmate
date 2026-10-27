import { expect, test } from "vitest";
import { importConnection, headerRecord, validateEndpoint, probeError } from "./connectionForm";
test("imports supported single HTTP declarations without exposing secrets", () => {
 expect(importConnection('{"mcpServers":{"reports":{"url":"https://example.com/mcp","headers":{"Authorization":"Bearer secret"}}}}')).toMatchObject({ name: "reports", url: "https://example.com/mcp", headers: [{ name: "Authorization", value: "Bearer secret" }] });
 expect(importConnection('{"servers":{"a":{"type":"sse","url":"https://example.com/sse"}}}').name).toBe("a");
 expect(importConnection('{"url":"https://example.com/mcp"}').name).toBe("");
});
test.each(['{"mcpServers":{"a":{},"b":{}}}', '{"url":"https://example.com","command":"node"}', '{"url":"https://example.com","oauth":{}}', '{"url":"https://example.com","headers":{"A":"${TOKEN}"}}', '{"url":"https://example.com","headers":{"A":"1","a":"2"}}', 'secret-invalid-json'])("rejects unsupported or ambiguous import safely", (input) => {
 expect(() => importConnection(input)).toThrow();
 try { importConnection(input); } catch (error) { expect(String(error)).not.toContain(input); }
});
test("rejects case-insensitive duplicate headers", () => {
 expect(() => headerRecord([{name:"Authorization",value:"a"},{name:"authorization",value:"b"}])).toThrow(/重复/);
});
test.each(["http://localhost/mcp", "http://127.0.0.1/mcp", "http://[::1]/mcp"])("explains server-side loopback rejection", url => {
 expect(() => validateEndpoint(url)).toThrow(/服务器/);
});
test("maps known error codes without reflecting raw remote errors", () => {
 expect(probeError("authentication_required")).toContain("认证");
 expect(probeError("secret-token")).not.toContain("secret-token");
});
test("rejects explicit SSE transport on unsupported URL path", () => {
 expect(() => importConnection('{"type":"sse","url":"https://example.com/events"}')).toThrow(/SSE 地址/);
});
test.each(["blocked_target", "dns_error", "connection_refused", "network_error", "tls_error", "timeout", "authentication_required", "permission_denied", "endpoint_not_found", "protocol_error", "worker_unavailable", "busy", "invalid_name", "invalid_url", "invalid_headers", "invalid_timeout", "invalid_configuration"])("has a safe message for %s", code => {
 expect(probeError(code)).not.toContain("操作未完成");
});
test("JSON duplicate keys cannot silently overwrite credentials", () => {
 expect(() => importConnection('{"url":"https://example.com/mcp","headers":{"Authorization":"a","Authorization":"b"}}')).toThrow(/重复/);
});
test.each(['{"type":["http"],"url":"https://example.com/mcp"}', '{"type":"http","url":"https://example.com/sse"}', '{"type":"http","url":"https://example.com/messages/"}'])("rejects invalid or conflicting explicit transport", input => {
 expect(() => importConnection(input)).toThrow();
});

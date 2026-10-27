"use client";

import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import markdown from "react-syntax-highlighter/dist/esm/languages/prism/markdown";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import { atomDark } from "react-syntax-highlighter/dist/esm/styles/prism";

SyntaxHighlighter.registerLanguage("bash", bash);
SyntaxHighlighter.registerLanguage("sh", bash);
SyntaxHighlighter.registerLanguage("javascript", javascript);
SyntaxHighlighter.registerLanguage("js", javascript);
SyntaxHighlighter.registerLanguage("json", json);
SyntaxHighlighter.registerLanguage("markdown", markdown);
SyntaxHighlighter.registerLanguage("md", markdown);
SyntaxHighlighter.registerLanguage("python", python);
SyntaxHighlighter.registerLanguage("py", python);
SyntaxHighlighter.registerLanguage("sql", sql);
SyntaxHighlighter.registerLanguage("typescript", typescript);
SyntaxHighlighter.registerLanguage("ts", typescript);

const SUPPORTED_LANGUAGES = new Set([
  "bash", "sh", "javascript", "js", "json", "markdown", "md", "python",
  "py", "sql", "typescript", "ts",
]);

export default function CodeHighlighter({
  value,
  language,
}: {
  value: string;
  language?: string;
}) {
  return (
    <SyntaxHighlighter
      language={language && SUPPORTED_LANGUAGES.has(language)
        ? language
        : "text"}
      style={atomDark}
      customStyle={{
        margin: 0,
        padding: "1rem",
        background: "#111318",
        fontSize: "12px",
        lineHeight: "1.5rem",
      }}
      codeTagProps={{ style: { fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace" } }}
      wrapLongLines={false}
    >
      {value}
    </SyntaxHighlighter>
  );
}

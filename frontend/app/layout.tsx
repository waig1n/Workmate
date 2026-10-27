import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "@/components/Providers";

export const metadata: Metadata = {
  title: "Workmate · 企业智能体工作台",
  description: "安全、可治理的企业智能体工作空间",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <body className="antialiased h-full">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}

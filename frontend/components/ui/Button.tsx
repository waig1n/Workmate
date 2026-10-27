import type { ButtonHTMLAttributes, ReactNode } from "react";

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
type ButtonSize = "sm" | "md" | "icon";

const variants: Record<ButtonVariant, string> = {
  primary: "bg-[var(--ink)] text-[var(--surface)] shadow-sm hover:-translate-y-0.5 hover:shadow-md",
  secondary: "border border-[var(--line-strong)] bg-[var(--surface-raised)] text-[var(--ink)] hover:bg-[var(--surface-subtle)]",
  ghost: "text-[var(--ink-soft)] hover:bg-[var(--surface-subtle)] hover:text-[var(--ink)]",
  danger: "border border-red-200 bg-white text-red-600 hover:bg-red-50 dark:border-red-900 dark:bg-transparent dark:hover:bg-red-950/40",
};

const sizes: Record<ButtonSize, string> = {
  sm: "h-9 rounded-xl px-3 text-xs",
  md: "h-11 rounded-[14px] px-4 text-sm",
  icon: "h-10 w-10 rounded-xl",
};

export function cx(...values: Array<string | false | null | undefined>) {
  return values.filter(Boolean).join(" ");
}

export function Button({ variant = "primary", size = "md", className, children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  children: ReactNode;
}) {
  return (
    <button className={cx("inline-flex shrink-0 items-center justify-center gap-2 font-semibold disabled:pointer-events-none disabled:opacity-40", variants[variant], sizes[size], className)} {...props}>
      {children}
    </button>
  );
}

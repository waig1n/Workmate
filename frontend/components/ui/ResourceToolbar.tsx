"use client";

import type { ReactNode } from "react";
import { cx } from "./Button";
import { Icon } from "./Icons";

export function ResourceToolbar<T extends string>({ label, options, value, onChange, searchLabel, placeholder, search, onSearch, actions, variant = "categories" }: {
  variant?: "categories" | "compact";
  label: string;
  options: readonly { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
  searchLabel: string;
  placeholder: string;
  search: string;
  onSearch: (value: string) => void;
  actions?: ReactNode;
}) {
  if (variant === "compact") return <div className="mb-5 flex flex-wrap items-center gap-3">
    <div className="relative w-full sm:w-80 sm:max-w-sm">
      <Icon name="search" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--ink-faint)]" />
      <input aria-label={searchLabel} placeholder={placeholder} value={search} onChange={event => onSearch(event.target.value)} className="product-control w-full pl-10 pr-3 text-sm" />
    </div>
    <select aria-label={label} value={value} onChange={event => onChange(event.target.value as T)} className="product-control px-3 text-sm">
      {options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
    </select>
    {actions && <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div>}
  </div>;
  return <div className="mb-5 space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div role="group" aria-label={label} className="flex max-w-full flex-wrap gap-1 rounded-xl bg-[var(--surface-subtle)] p-1">
        {options.map(option => <button key={option.value} type="button" aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
          className={cx("min-h-9 rounded-lg px-3 py-2 text-sm transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]", value === option.value ? "bg-[var(--surface-raised)] font-medium text-[var(--accent-strong)] shadow-sm" : "text-[var(--ink-soft)] hover:text-[var(--ink)]")}>{option.label}</button>)}
      </div>
      {actions && <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
    <div className="relative w-full sm:max-w-sm">
      <Icon name="search" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--ink-faint)]" />
      <input aria-label={searchLabel} placeholder={placeholder} value={search} onChange={event => onSearch(event.target.value)} className="product-control w-full pl-10 pr-3 text-sm" />
    </div>
  </div>;
}

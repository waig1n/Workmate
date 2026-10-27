"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { ArtifactViewer } from "@/components/artifacts/ArtifactViewer";
import { Icon } from "@/components/ui/Icons";
import { PanelResizeHandle } from "@/components/ui/PanelResizeHandle";
import { groupArtifactVersions } from "@/lib/artifacts/versions";
import type { Artifact } from "@/lib/types";

import { ARTIFACT_PANEL_MIN_WIDTH, ARTIFACT_PANEL_BASE_MAX_WIDTH } from "@/lib/useArtifactPanelWidth";

export function WorkbenchSidecar({
  artifacts,
  width,
  maxWidth = ARTIFACT_PANEL_BASE_MAX_WIDTH,
  onResize,
  onResetWidth,
  onClose,
  focusArtifactId = null,
  focusRequest = 0,
}: {
  artifacts: Artifact[];
  width: number;
  maxWidth?: number;
  onResize: (delta: number) => void;
  onResetWidth: () => void;
  onClose: () => void;
  focusArtifactId?: string | null;
  focusRequest?: number;
}) {
  const panelRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  const series = useMemo(() => groupArtifactVersions(artifacts), [artifacts]);
  const [selectedSeriesKey, setSelectedSeriesKey] = useState<string | null>(null);
  // null means "follow the latest revision" for the selected series.
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const selectedSeries = series.find((item) => item.key === selectedSeriesKey) ?? series[0] ?? null;
  const selected = selectedSeries?.versions.find((artifact) => artifact.id === selectedVersionId)
    ?? selectedSeries?.latest
    ?? null;

  useEffect(() => {
    if (!selectedSeriesKey || !series.some((item) => item.key === selectedSeriesKey)) {
      setSelectedSeriesKey(series[0]?.key ?? null);
      setSelectedVersionId(null);
    } else if (selectedVersionId && !selectedSeries?.versions.some((artifact) => artifact.id === selectedVersionId)) {
      setSelectedVersionId(null);
    }
  }, [selectedSeries, selectedSeriesKey, selectedVersionId, series]);

  useEffect(() => {
    if (!focusArtifactId) return;
    const targetSeries = series.find((item) => item.versions.some((artifact) => artifact.id === focusArtifactId));
    if (!targetSeries) return;
    setSelectedSeriesKey(targetSeries.key);
    setSelectedVersionId(focusArtifactId === targetSeries.latest.id ? null : focusArtifactId);
  }, [focusArtifactId, focusRequest, series]);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previousFocus?.focus();
    };
  }, []);

  return (
    <>
      <button type="button" aria-label="关闭侧栏" className="popup-backdrop-enter fixed inset-0 z-40 bg-[#15130f]/30 backdrop-blur-[2px] xl:hidden" onClick={onClose} />
      <aside
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-label="会话工作台侧栏"
        style={{ "--artifact-panel-width": `${width}px` } as React.CSSProperties}
        className="popup-sidecar-enter fixed inset-y-3 right-3 z-50 flex w-[min(94vw,var(--artifact-panel-width))] flex-col overflow-visible rounded-[22px] border border-[var(--line)] bg-[var(--surface-subtle)] shadow-[var(--shadow-md)] outline-none xl:relative xl:inset-auto xl:z-auto xl:shrink-0 xl:rounded-none xl:border-y-0 xl:border-r-0 xl:shadow-none"
      >
        <PanelResizeHandle edge="left" width={width} minWidth={ARTIFACT_PANEL_MIN_WIDTH} maxWidth={maxWidth} label="调整成果栏宽度" onResize={onResize} onReset={onResetWidth} className="hidden lg:flex" />
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-[inherit]">
          <div className="flex h-14 shrink-0 items-center border-b border-[var(--line)] bg-[var(--surface)] px-3">
            <p className="flex-1 px-1 text-[11px] font-semibold">成果工作台</p>
            <span className="mr-2 text-[9px] text-[var(--ink-faint)]">{series.length ? `${series.length} 项成果` : "等待成果"}</span>
            <button type="button" aria-label="关闭侧栏" onClick={onClose} className="grid h-8 w-8 place-items-center rounded-lg text-[var(--ink-faint)] hover:bg-[var(--surface-subtle)] hover:text-[var(--ink)]"><Icon name="x" className="h-3.5 w-3.5" /></button>
          </div>
          <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
            {series.length > 1 && <div className="flex gap-1.5 overflow-x-auto border-b border-[var(--line)] bg-[var(--surface)] p-2">{series.map((item) => <button key={item.key} type="button" title={item.name} onClick={() => { setSelectedSeriesKey(item.key); setSelectedVersionId(null); }} className={`max-w-[190px] shrink-0 truncate rounded-lg border px-2.5 py-1.5 text-[9px] ${item.key === selectedSeries?.key ? "border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent-strong)]" : "border-[var(--line)] bg-[var(--surface-raised)] text-[var(--ink-soft)]"}`}>{item.name}<span className="ml-1 opacity-60">v{item.latest.version}</span></button>)}</div>}
            {selectedVersionId && selectedSeries && selectedVersionId !== selectedSeries.latest.id && <button type="button" onClick={() => setSelectedVersionId(null)} className="flex items-center justify-center gap-1.5 border-b border-[var(--line)] bg-[var(--accent-soft)] px-3 py-2 text-[10px] font-medium text-[var(--accent-strong)]">已有最新版本 v{selectedSeries.latest.version}，点击查看<Icon name="arrow" className="h-3 w-3" /></button>}
            <ArtifactViewer artifact={selected} versions={selectedSeries?.versions} onVersionChange={setSelectedVersionId} />
          </div>
        </div>
      </aside>
    </>
  );
}

"use client";

import { useEffect, useState } from "react";
import { usePersistentPanelWidth } from "./usePersistentPanelWidth";

export const ARTIFACT_PANEL_DEFAULT_WIDTH = 460;
export const ARTIFACT_PANEL_MIN_WIDTH = 340;
export const ARTIFACT_PANEL_BASE_MAX_WIDTH = 560;

export function useArtifactPanelWidth() {
  const [maxWidth, setMaxWidth] = useState(ARTIFACT_PANEL_BASE_MAX_WIDTH);
  useEffect(() => {
    const resize = () => setMaxWidth(Math.max(ARTIFACT_PANEL_BASE_MAX_WIDTH, Math.floor(window.innerWidth / 2)));
    resize();
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  const panel = usePersistentPanelWidth({
    storageKey: "workmate:artifact-panel-width",
    defaultWidth: ARTIFACT_PANEL_DEFAULT_WIDTH,
    minWidth: ARTIFACT_PANEL_MIN_WIDTH,
    maxWidth,
  });
  return { ...panel, maxWidth };
}

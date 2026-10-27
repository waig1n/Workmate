import type { Artifact } from "@/lib/types";

export interface ArtifactSeries {
  key: string;
  name: string;
  latest: Artifact;
  versions: Artifact[];
}
/**
 * Group the current minimum artifact identity: one exact output path inside
 * one Conversation. The backend already scopes the supplied artifacts to a
 * Conversation; a future artifact_series_id can replace this key without
 * changing the Sidecar interaction model.
 */
export function groupArtifactVersions(artifacts: readonly Artifact[]): ArtifactSeries[] {
  const groups = new Map<string, Artifact[]>();
  for (const artifact of artifacts) {
    const versions = groups.get(artifact.name) ?? [];
    const index = versions.findIndex((version) => version.id === artifact.id);
    if (index < 0) versions.push(artifact);
    else if (Date.parse(artifact.created_at) > Date.parse(versions[index].created_at)) versions[index] = artifact;
    groups.set(artifact.name, versions);
  }

  return [...groups.entries()]
    .map(([name, versions]) => {
      const sorted = [...versions].sort((left, right) => (
        right.version - left.version
        || Date.parse(right.created_at) - Date.parse(left.created_at)
      ));
      return { key: name, name, latest: sorted[0], versions: sorted };
    })
    .sort((left, right) => (
      Date.parse(right.latest.created_at) - Date.parse(left.latest.created_at)
      || left.name.localeCompare(right.name)
    ));
}

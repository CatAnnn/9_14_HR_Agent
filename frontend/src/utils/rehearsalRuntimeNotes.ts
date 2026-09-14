export function normalizeRuntimeNoteDrafts(drafts: readonly string[]): string[] {
  const normalized: string[] = [];
  const seen = new Set<string>();

  for (const value of drafts) {
    const note = value.trim();
    if (!note || seen.has(note)) continue;
    normalized.push(note);
    seen.add(note);
  }

  return normalized;
}

export function runtimeNotesToDrafts(
  notes: readonly string[] | null | undefined,
): string[] {
  const normalized = normalizeRuntimeNoteDrafts(notes || []);
  return normalized.length ? normalized : [''];
}

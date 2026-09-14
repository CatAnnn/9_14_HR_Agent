/** Keep movement inside the background's 14% overscan on either edge. */
export function backgroundParallaxOffset(
  sectionTop: number,
  sectionHeight: number,
  viewportHeight: number,
): number {
  if (![sectionTop, sectionHeight, viewportHeight].every(Number.isFinite)
    || sectionHeight <= 0 || viewportHeight <= 0) return 0;

  const progress = Math.min(1, Math.max(0,
    (viewportHeight - sectionTop) / (viewportHeight + sectionHeight),
  ));
  return (progress - 0.5) * sectionHeight * 0.2;
}

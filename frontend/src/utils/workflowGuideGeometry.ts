export interface WorkflowGuideSpotlightInput {
  viewportWidth: number;
  viewportHeight: number;
  focusBottom: number;
  nextModuleTop?: number | null;
}

export interface WorkflowGuideSpotlightGeometry {
  top: number;
  left: number;
  width: number;
  height: number;
}

function finiteOr(value: number, fallback: number) {
  return Number.isFinite(value) ? value : fallback;
}

export function calculateWorkflowGuideSpotlight(
  input: WorkflowGuideSpotlightInput,
): WorkflowGuideSpotlightGeometry {
  const viewportWidth = Math.max(1, Math.floor(finiteOr(input.viewportWidth, 1)));
  const viewportHeight = Math.max(1, Math.floor(finiteOr(input.viewportHeight, 1)));
  const focusBottom = Math.max(1, Math.ceil(finiteOr(input.focusBottom, 1)));
  const moduleTop = input.nextModuleTop == null
    ? focusBottom
    : Math.floor(finiteOr(input.nextModuleTop, focusBottom));
  const bottom = Math.min(viewportHeight, Math.max(1, moduleTop));

  return {
    top: 0,
    left: 0,
    width: viewportWidth,
    height: bottom,
  };
}

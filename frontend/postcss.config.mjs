const REFERENCE_FONT_SIZE = 16;
const RESPONSIVE_SCALE_FILE = /\/responsive-scale\.css$/;
const PROJECT_STYLES = /\/frontend\/src\/styles\//;
const PX_VALUE = /(-?(?:\d+\.?\d*|\.\d+))px\b/g;
const MAX_WIDTH = /max-width\s*:\s*(\d+(?:\.\d+)?)px/i;
const DESKTOP_MIN_WIDTH = 960;

function formatRem(value) {
  return Number((value / REFERENCE_FONT_SIZE).toFixed(6)).toString();
}

export function convertPixelsToRem(value) {
  if (!value.includes('px') || value.includes('url(')) return value;

  return value.replace(PX_VALUE, (match, rawValue) => {
    const pixels = Number(rawValue);
    if (!Number.isFinite(pixels)) return match;
    if (pixels === 0) return '0';
    if (Math.abs(pixels) === 1) return match;
    return `${formatRem(pixels)}rem`;
  });
}

function constrainDesktopBreakpoint(params) {
  return params
    .split(',')
    .map((query) => {
      const match = query.match(MAX_WIDTH);
      if (!match || Number(match[1]) < DESKTOP_MIN_WIDTH) return query;
      if (query.includes(`max-width: ${DESKTOP_MIN_WIDTH - 1}px`)) return query;
      return `${query.trim()} and (max-width: ${DESKTOP_MIN_WIDTH - 1}px)`;
    })
    .join(', ');
}

function isProjectStyle(node) {
  const file = node.source?.input.file?.replaceAll('\\', '/');
  return Boolean(file && PROJECT_STYLES.test(file) && !RESPONSIVE_SCALE_FILE.test(file));
}

export function createProportionalRemPlugin() {
  return {
    postcssPlugin: 'hr-agent-proportional-rem',
    Declaration(declaration) {
      if (!isProjectStyle(declaration)) return;
      declaration.value = convertPixelsToRem(declaration.value);
    },
    AtRule(atRule) {
      if (atRule.name !== 'media' || !isProjectStyle(atRule)) return;
      atRule.params = constrainDesktopBreakpoint(atRule.params);
    },
  };
}

export default {
  plugins: [createProportionalRemPlugin()],
};

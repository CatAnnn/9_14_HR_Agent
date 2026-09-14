import type { Contents } from 'epubjs';


const EXTERNAL_URL = /^(?:https?:)?\/\//i;
const FORBIDDEN_ELEMENTS = [
  'script',
  'iframe',
  'frame',
  'frameset',
  'form',
  'object',
  'embed',
  'input',
  'button',
  'textarea',
  'select',
].join(', ');

interface SerializedSection {
  output: string;
}

function sanitizeDocument(document: Document): void {
  document.querySelectorAll(FORBIDDEN_ELEMENTS).forEach((element) => element.remove());

  if (!document.querySelector('meta[http-equiv="Content-Security-Policy" i]')) {
    const csp = document.createElement('meta');
    csp.setAttribute('http-equiv', 'Content-Security-Policy');
    csp.setAttribute('content', [
      "default-src 'none'",
      "img-src data: blob:",
      "style-src 'unsafe-inline' blob:",
      "font-src data: blob:",
      "media-src data: blob:",
    ].join('; '));
    document.head?.prepend(csp);
  }

  document.querySelectorAll<HTMLElement>('[src], [srcset], [poster]').forEach((element) => {
    for (const attribute of ['src', 'srcset', 'poster']) {
      const value = element.getAttribute(attribute)?.trim();
      if (value && EXTERNAL_URL.test(value)) element.removeAttribute(attribute);
    }
  });
  document.querySelectorAll<HTMLAnchorElement>('a[href]').forEach((anchor) => {
    const href = anchor.getAttribute('href')?.trim() || '';
    anchor.removeAttribute('target');
    anchor.removeAttribute('rel');
    if (EXTERNAL_URL.test(href) || /^(?:javascript|data):/i.test(href)) {
      anchor.removeAttribute('href');
      anchor.setAttribute('aria-disabled', 'true');
    }
  });
}

export function sanitizeEpubContents(contents: Contents): void {
  sanitizeDocument(contents.document);
}

export function sanitizeSerializedEpubSection(
  output: string,
  section: SerializedSection,
): void {
  const source = section.output || output;
  const parser = new DOMParser();
  const xmlDocument = parser.parseFromString(source, 'application/xhtml+xml');
  const document = xmlDocument.querySelector('parsererror')
    ? parser.parseFromString(source, 'text/html')
    : xmlDocument;
  sanitizeDocument(document);
  section.output = new XMLSerializer().serializeToString(document);
}

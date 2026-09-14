import { getApiBase } from '../utils/format';
import { getCurrentLanguage } from '../i18n/LanguageContext';


export type EbookMetricEvent =
  | 'reader_open'
  | 'reader_ready'
  | 'page_change'
  | 'progress_restored'
  | 'reader_error'
  | 'download';

export interface EbookDownload {
  blob: Blob;
  filename: string;
}

function reportUnauthorized(response: Response): void {
  if (response.status === 401) {
    window.dispatchEvent(new Event('hr-auth-expired'));
  }
}

async function errorDetail(response: Response): Promise<string> {
  try {
    const payload = await response.json() as { detail?: unknown };
    return typeof payload.detail === 'string' ? payload.detail : '电子书请求失败。';
  } catch {
    return '电子书请求失败。';
  }
}

function responseFilename(response: Response, fallback: string): string {
  const disposition = response.headers.get('content-disposition') || '';
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    try {
      return decodeURIComponent(encoded);
    } catch {
      return encoded;
    }
  }
  return disposition.match(/filename="?([^";]+)"?/i)?.[1] || fallback;
}

export function ebookFileUrl(bookId: string, download = false): string {
  const path = `/resources/ebooks/${encodeURIComponent(bookId)}/file`;
  return `${getApiBase()}${path}${download ? '?download=true' : ''}`;
}

export async function loadEbookArchive(
  bookId: string,
  signal?: AbortSignal,
): Promise<ArrayBuffer> {
  const response = await fetch(ebookFileUrl(bookId), {
    credentials: 'include',
    headers: {
      Accept: 'application/epub+zip',
      'Accept-Language': getCurrentLanguage(),
    },
    signal,
  });
  reportUnauthorized(response);
  if (!response.ok) throw new Error(await errorDetail(response));
  const archive = await response.arrayBuffer();
  const signature = new Uint8Array(archive, 0, Math.min(4, archive.byteLength));
  const zipMarker = signature.length >= 4
    ? (signature[2] << 8) | signature[3]
    : -1;
  if (
    signature.length < 4
    || signature[0] !== 0x50
    || signature[1] !== 0x4b
    || ![0x0304, 0x0506, 0x0708].includes(zipMarker)
  ) {
    throw new Error('电子书文件不是有效的 EPUB。');
  }
  return archive;
}

export async function downloadEbook(bookId: string): Promise<EbookDownload> {
  const response = await fetch(ebookFileUrl(bookId, true), {
    credentials: 'include',
    headers: { 'Accept-Language': getCurrentLanguage() },
  });
  reportUnauthorized(response);
  if (!response.ok) throw new Error(await errorDetail(response));
  return {
    blob: await response.blob(),
    filename: responseFilename(response, `${bookId}.epub`),
  };
}

export async function recordEbookEvent(
  bookId: string,
  event: EbookMetricEvent,
  durationMs?: number,
): Promise<void> {
  const response = await fetch(
    `${getApiBase()}/resources/ebooks/${encodeURIComponent(bookId)}/events`,
    {
      method: 'POST',
      credentials: 'include',
      keepalive: true,
      headers: {
        'Content-Type': 'application/json',
        'Accept-Language': getCurrentLanguage(),
      },
      body: JSON.stringify({
        event,
        duration_ms: Number.isFinite(durationMs) ? durationMs : null,
      }),
    },
  );
  reportUnauthorized(response);
  if (!response.ok && response.status !== 401) {
    throw new Error(await errorDetail(response));
  }
}

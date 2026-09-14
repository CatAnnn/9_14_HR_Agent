import rawCatalog from '../../../Ebook/catalog.json';


export interface EbookCatalogEntry {
  id: string;
  sourceFile: string;
  title: string;
  authors: readonly string[];
  publisher: string | null;
  publishedAt: string | null;
  summary: string;
  tags: readonly string[];
  cover: string;
}

interface RawEbookCatalogEntry {
  id: unknown;
  source_file: unknown;
  title: unknown;
  authors: unknown;
  publisher: unknown;
  published_at: unknown;
  summary: unknown;
  tags: unknown;
  cover: unknown;
}

interface RawEbookCatalog {
  version: unknown;
  items: unknown;
}

const EBOOK_ID_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const EBOOK_COVER_PREFIX = '/assets/resource/ebooks/covers/';

function requireText(value: unknown, field: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`Ebook catalog field "${field}" must not be empty.`);
  }
  return value.trim();
}

function optionalText(value: unknown, field: string): string | null {
  if (value === null) return null;
  return requireText(value, field);
}

function requireTextList(value: unknown, field: string): readonly string[] {
  if (!Array.isArray(value) || value.length === 0) {
    throw new Error(`Ebook catalog field "${field}" must be a non-empty list.`);
  }
  return value.map((item, index) => requireText(item, `${field}.${index}`));
}

function validateCatalog(value: unknown): readonly EbookCatalogEntry[] {
  const catalog = value as RawEbookCatalog;
  if (!catalog || catalog.version !== 1 || !Array.isArray(catalog.items)) {
    throw new Error('Ebook catalog must use version 1 and contain an items list.');
  }

  const seen = new Set<string>();
  return catalog.items.map((candidate, index) => {
    const raw = candidate as RawEbookCatalogEntry;
    const id = requireText(raw.id, `items.${index}.id`);
    if (!EBOOK_ID_PATTERN.test(id) || seen.has(id)) {
      throw new Error(`Ebook catalog contains an invalid or duplicate id "${id}".`);
    }
    seen.add(id);

    const sourceFile = requireText(raw.source_file, `${id}.source_file`);
    if (sourceFile.includes('/') || sourceFile.includes('\\')) {
      throw new Error(`Ebook catalog source path for "${id}" must stay inside Ebook/.`);
    }
    const cover = requireText(raw.cover, `${id}.cover`);
    if (!cover.startsWith(EBOOK_COVER_PREFIX)) {
      throw new Error(`Ebook catalog cover for "${id}" must use a local ebook asset.`);
    }

    return {
      id,
      sourceFile,
      title: requireText(raw.title, `${id}.title`),
      authors: requireTextList(raw.authors, `${id}.authors`),
      publisher: optionalText(raw.publisher, `${id}.publisher`),
      publishedAt: optionalText(raw.published_at, `${id}.published_at`),
      summary: requireText(raw.summary, `${id}.summary`),
      tags: requireTextList(raw.tags, `${id}.tags`),
      cover,
    } satisfies EbookCatalogEntry;
  });
}

export const ebookCatalog = validateCatalog(rawCatalog);

const ebookById = new Map(ebookCatalog.map((entry) => [entry.id, entry]));

export function findEbook(bookId: string | undefined): EbookCatalogEntry | undefined {
  return bookId ? ebookById.get(bookId) : undefined;
}

export function ebookDetailPath(bookId: string): string {
  return `/resource/book/${encodeURIComponent(bookId)}`;
}

export function ebookReaderPath(bookId: string): string {
  return `${ebookDetailPath(bookId)}/read`;
}

export function ebookPublishedLabel(publishedAt: string | null): string | undefined {
  return publishedAt?.split('-').join('.');
}

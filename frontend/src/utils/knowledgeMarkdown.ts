export interface KnowledgeInlineToken {
  type: 'text' | 'strong' | 'emphasis' | 'code' | 'link' | 'strike';
  text: string;
  href?: string;
}

export interface KnowledgeQuoteBlock {
  type: 'heading' | 'paragraph' | 'bullet' | 'ordered' | 'quote';
  text: string;
  inline: KnowledgeInlineToken[];
  marker?: string;
}

function decodeHtmlEntities(value: string) {
  const namedEntities: Record<string, string> = {
    amp: '&',
    apos: "'",
    gt: '>',
    lt: '<',
    nbsp: ' ',
    quot: '"',
  };
  return value.replace(
    /&(#x[\da-f]+|#\d+|amp|apos|gt|lt|nbsp|quot);/gi,
    (match, entity: string) => {
      const normalized = entity.toLowerCase();
      if (normalized.startsWith('#x')) {
        const codePoint = Number.parseInt(normalized.slice(2), 16);
        return Number.isFinite(codePoint) ? String.fromCodePoint(codePoint) : match;
      }
      if (normalized.startsWith('#')) {
        const codePoint = Number.parseInt(normalized.slice(1), 10);
        return Number.isFinite(codePoint) ? String.fromCodePoint(codePoint) : match;
      }
      return namedEntities[normalized] || match;
    },
  );
}

function plainInlineText(value: string) {
  return value
    .replace(/[\u200B-\u200D\u2060\uFEFF]/g, '')
    .replace(/\u00A0/g, ' ')
    .replace(/!\[([^\]]*)\]\((?:[^()\n]|\([^()\n]*\))*\)/g, '$1')
    .replace(/\[([^\]]+)\]\((?:[^()\n]|\([^()\n]*\))*\)/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/~~([^~]+)~~/g, '$1')
    .replace(/\*([^*\n]+)\*/g, '$1')
    .replace(/\u0060([^\u0060]+)\u0060/g, '$1')
    .replace(/^>\s*/, '')
    .replace(/\\([\\\u0060*_[\]{}()#+\-.!>])/g, '$1')
    .replace(/\s+/g, ' ')
    .trim();
}

function plainTextFragment(value: string) {
  return value
    .replace(/[\u200B-\u200D\u2060\uFEFF]/g, '')
    .replace(/\u00A0/g, ' ')
    .replace(/\\([\\\u0060*_[\]{}()#+\-.!>])/g, '$1')
    .replace(/\s+/g, ' ');
}

function safeLink(value: string) {
  const href = decodeHtmlEntities(String(value || '').trim());
  return /^https?:\/\//i.test(href) ? href : '';
}

function compileInline(value: string): KnowledgeInlineToken[] {
  const source = String(value || '');
  const pattern = /(!?\[[^\]\n]*\]\((?:[^()\n]|\([^()\n]*\))*\)|\*\*[^*\n]+\*\*|__[^_\n]+__|~~[^~\n]+~~|\u0060[^\u0060\n]+\u0060|\*[^*\n]+\*)/g;
  const tokens: KnowledgeInlineToken[] = [];
  let cursor = 0;

  const append = (token: KnowledgeInlineToken) => {
    if (!token.text) return;
    const previous = tokens[tokens.length - 1];
    if (previous?.type === 'text' && token.type === 'text') {
      previous.text += token.text;
      return;
    }
    tokens.push(token);
  };

  for (const match of source.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > cursor) {
      append({ type: 'text', text: plainTextFragment(source.slice(cursor, index)) });
    }

    const token = match[0];
    const image = token.match(/^!\[([^\]]*)\]\(((?:[^()\n]|\([^()\n]*\))*)\)$/);
    const link = token.match(/^\[([^\]]+)\]\(((?:[^()\n]|\([^()\n]*\))*)\)$/);
    if (image) {
      append({ type: 'text', text: plainInlineText(image[1]) });
    } else if (link) {
      const text = plainInlineText(link[1]);
      const href = safeLink(link[2]);
      append(href ? { type: 'link', text, href } : { type: 'text', text });
    } else if (token.startsWith('**') || token.startsWith('__')) {
      append({ type: 'strong', text: plainInlineText(token.slice(2, -2)) });
    } else if (token.startsWith('~~')) {
      append({ type: 'strike', text: plainInlineText(token.slice(2, -2)) });
    } else if (token.startsWith('\u0060')) {
      append({ type: 'code', text: token.slice(1, -1).trim() });
    } else {
      append({ type: 'emphasis', text: plainInlineText(token.slice(1, -1)) });
    }
    cursor = index + token.length;
  }

  if (cursor < source.length) {
    append({ type: 'text', text: plainTextFragment(source.slice(cursor)) });
  }
  if (tokens.length) {
    tokens[0].text = tokens[0].text.replace(/^\s+/, '');
    tokens[tokens.length - 1].text = tokens[tokens.length - 1].text.replace(/\s+$/, '');
  }
  const visibleTokens = tokens.filter((token) => token.text);
  return visibleTokens.length
    ? visibleTokens
    : [{ type: 'text', text: plainInlineText(source) }];
}

function block(
  type: KnowledgeQuoteBlock['type'],
  markdown: string,
  marker?: string,
): KnowledgeQuoteBlock {
  return {
    type,
    text: plainInlineText(markdown),
    inline: compileInline(markdown),
    ...(marker ? { marker } : {}),
  };
}

function splitReadableParagraph(value: string) {
  const sentences = value.match(/[^。！？.!?；;]+[。！？.!?；;]?/g) || [value];
  const paragraphs: string[] = [];
  let current = '';

  for (const sentence of sentences) {
    const normalizedSentence = sentence.trim();
    const separator = current
      && /[.!?;:]$/u.test(plainInlineText(current))
      && /^[\p{L}\p{N}]/u.test(plainInlineText(normalizedSentence))
      ? ' '
      : '';
    const candidate = current + separator + normalizedSentence;
    if (current && plainInlineText(candidate).length > 240) {
      paragraphs.push(current);
      current = normalizedSentence;
    } else {
      current = candidate;
    }
  }
  if (current) paragraphs.push(current);
  return paragraphs;
}

function normalizeSource(value: string) {
  return decodeHtmlEntities(
    String(value || '')
      .replace(/<img\b[^>]*>/gi, ' ')
      .replace(/<br\s*\/?>/gi, '\n')
      .replace(/<h[1-6]\b[^>]*>/gi, '\n## ')
      .replace(/<(?:strong|b)\b[^>]*>/gi, '**')
      .replace(/<\/(?:strong|b)>/gi, '**')
      .replace(/<(?:em|i)\b[^>]*>/gi, '*')
      .replace(/<\/(?:em|i)>/gi, '*')
      .replace(/<blockquote\b[^>]*>/gi, '\n> ')
      .replace(/<code\b[^>]*>/gi, '\u0060')
      .replace(/<\/code>/gi, '\u0060')
      .replace(/<li\b[^>]*>/gi, '\n- ')
      .replace(/<\/(?:td|th)>/gi, ' | ')
      .replace(
        /<\/(?:tr|p|div|section|article|h[1-6]|li|ul|ol|table|blockquote)>/gi,
        '\n',
      )
      .replace(/<[^>]+>/g, ' ')
      .replace(/([.!?])(?=[A-Z])/g, '$1 '),
  ).replace(/[ \t]+[\/／][ \t]+/g, '\n');
}

export function compileKnowledgeMarkdown(value: string): KnowledgeQuoteBlock[] {
  const source = normalizeSource(value);
  const blocks: KnowledgeQuoteBlock[] = [];
  const append = (nextBlock: KnowledgeQuoteBlock) => {
    if (!nextBlock.text) return;
    const previous = blocks[blocks.length - 1];
    if (previous?.type === nextBlock.type && previous.text === nextBlock.text) return;
    blocks.push(nextBlock);
  };

  for (const rawLine of source.split(/\r?\n/)) {
    const trimmed = rawLine.trim();
    if (!trimmed || /^\u0060{3}/.test(trimmed)) continue;
    if (/^\|?[\s:|-]+\|[\s:|-|]*$/.test(trimmed)) continue;

    const heading = trimmed.match(/^#{1,6}\s+(.+)$/);
    if (heading) {
      append(block('heading', heading[1]));
      continue;
    }
    const bullet = trimmed.match(/^(?:[-+*•])\s+(.+)$/);
    if (bullet) {
      append(block('bullet', bullet[1]));
      continue;
    }
    const ordered = trimmed.match(
      /^(\d+[.)]|[一二三四五六七八九十]+[、）)])\s*(.+)$/,
    );
    if (ordered) {
      append(block('ordered', ordered[2], ordered[1]));
      continue;
    }
    const quoteLine = trimmed.match(/^>\s*(.+)$/);
    if (quoteLine) {
      append(block('quote', quoteLine[1]));
      continue;
    }
    if (trimmed.includes('|')) {
      const cells = trimmed.split('|').map((cell) => cell.trim()).filter(Boolean);
      if (cells.length > 1) {
        append(block('bullet', cells.join(' · ')));
        continue;
      }
    }
    for (const paragraph of splitReadableParagraph(trimmed)) {
      append(block('paragraph', paragraph));
    }
  }

  if (!blocks.length) {
    const fallback = block('paragraph', source);
    if (fallback.text) blocks.push(fallback);
  }
  return blocks;
}

export function withKnowledgeBlockText(
  source: KnowledgeQuoteBlock,
  text: string,
): KnowledgeQuoteBlock {
  return {
    ...source,
    text,
    inline: compileInline(text),
  };
}

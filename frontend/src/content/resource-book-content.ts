import { ebookCatalog, ebookDetailPath } from './ebook-content';

const RESOURCE_ASSET_BASE = '/assets/resource';

export interface ResourceBookCard {
  id: string;
  title: string;
  titleEnglish: string;
  summary: string;
  summaryEnglish: string;
  authors: string;
  tags: readonly string[];
  image: string;
  imageAlt: string;
  href: string;
}

const EBOOK_SEARCH_ENGLISH_BY_ID: Readonly<
  Record<string, { title: string; summary: string }>
> = {
  'nonviolent-communication': {
    title: 'Nonviolent Communication',
    summary: 'Use observations, feelings, needs, and requests to communicate clearly and empathetically without escalating conflict.',
  },
  'the-art-of-communication': {
    title: 'The Art of Communication',
    summary: 'Understand communication through self-awareness, listening, verbal and nonverbal expression, relationships, and conflict practice.',
  },
  'never-split-the-difference': {
    title: 'Never Split the Difference',
    summary: 'Apply tactical empathy, mirroring, calibrated questions, and emotion labeling to move difficult negotiations forward.',
  },
  'harvard-classic-negotiation': {
    title: 'Harvard Classic Negotiation',
    summary: 'Use decision science and behavioral research to improve information gathering, value creation, agreement design, and bias awareness.',
  },
  'conflict-resolution-skills': {
    title: 'Conflict Resolution Skills',
    summary: 'Recognize conflict patterns, separate facts from interpretations, and respond more effectively in high-tension interactions.',
  },
  'influence': {
    title: 'Influence',
    summary: 'Understand the principles behind persuasion and learn to recognize and use influence responsibly.',
  },
  'ted-talks-speaking': {
    title: 'TED Talks: The Official TED Guide to Public Speaking',
    summary: 'Shape worthwhile ideas into clear, persuasive presentations through topic focus, narrative, explanation, and delivery.',
  },
  'deliberate-practice': {
    title: 'Deliberate Practice',
    summary: 'Build expertise through clear goals, focused practice, immediate feedback, and continuous correction.',
  },
  'enneagram': {
    title: 'The Enneagram',
    summary: 'Explore nine personality patterns, motivations, and interaction tendencies to improve self-awareness and collaboration.',
  },
};

export interface ResourceBookPageContent {
  hero: {
    title: string;
    description: string;
    ctaLabel: string;
    desktopImage: string;
    mobileImage: string;
  };
  books: {
    title: string;
    description: string;
    items: readonly ResourceBookCard[];
  };
  contact: {
    title: string;
    description: string;
  };
}

function validateBookContent(content: ResourceBookPageContent): ResourceBookPageContent {
  const ids = new Set<string>();
  for (const item of content.books.items) {
    if (!item.id.trim() || ids.has(item.id)) {
      throw new Error(`Invalid or duplicate ebook resource ID "${item.id}".`);
    }
    if (
      !item.title.trim()
      || !item.titleEnglish.trim()
      || !item.summary.trim()
      || !item.summaryEnglish.trim()
      || !item.imageAlt.trim()
    ) {
      throw new Error(`Ebook resource "${item.id}" is missing display content.`);
    }
    if (!item.image.startsWith(`${RESOURCE_ASSET_BASE}/`)) {
      throw new Error(`Ebook resource "${item.id}" must use a local cover image.`);
    }
    if (!item.href.startsWith('/resource/book/')) {
      throw new Error(`Ebook resource "${item.id}" must use an ebook detail route.`);
    }
    ids.add(item.id);
  }
  return content;
}

const content = {
  hero: {
    title: '资源中心',
    description: '阅读沟通、谈判、影响力与个人发展的精选书籍。',
    ctaLabel: '浏览书籍',
    desktopImage: `${RESOURCE_ASSET_BASE}/resources.webp`,
    mobileImage: `${RESOURCE_ASSET_BASE}/resources.webp`,
  },
  books: {
    title: '书籍',
    description: '选择书籍查看简介、在线阅读或下载 EPUB',
    items: ebookCatalog.map((book) => {
      const english = EBOOK_SEARCH_ENGLISH_BY_ID[book.id];
      if (!english) throw new Error(`Ebook resource "${book.id}" is missing English search metadata.`);
      return {
        id: book.id,
        title: book.title,
        titleEnglish: english.title,
        summary: book.summary,
        summaryEnglish: english.summary,
        authors: book.authors.join('、'),
        tags: book.tags,
        image: book.cover,
        imageAlt: `${book.title}书籍封面`,
        href: ebookDetailPath(book.id),
      };
    }),
  },
  contact: {
    title: '把阅读转化为真实沟通行动',
    description: '从书中选择适合当前管理场景的方法，并在沟通工作台中完成准备与演练。',
  },
} satisfies ResourceBookPageContent;

export const resourceBookPageContent = validateBookContent(content);

import ePub, {
  type Book,
  type Contents,
  type Location,
  type NavItem,
  type Rendition,
} from 'epubjs';
import {
  ArrowLeft,
  BookOpen,
  ChevronLeft,
  ChevronRight,
  Download,
  List,
  LoaderCircle,
  Minus,
  Plus,
  Rows3,
  Settings2,
  Type,
  X,
} from 'lucide-react';
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  downloadEbook,
  ebookFileUrl,
  loadEbookArchive,
  recordEbookEvent,
} from '../api/ebooks';
import {
  ebookDetailPath,
  findEbook,
} from '../content/ebook-content';
import { resourceBookPageContent } from '../content/resource-book-content';
import { useLanguage } from '../i18n/LanguageContext';
import { useAuthStore } from '../store/authStore';
import {
  sanitizeEpubContents,
  sanitizeSerializedEpubSection,
} from '../utils/epubSecurity';
import '../styles/ebook-reader.css';


type ReaderMode = 'paginated' | 'scrolled';
type ReaderTheme = 'light' | 'soft' | 'dark';
type ReaderLineHeight = 'compact' | 'comfortable' | 'relaxed';
type ReaderErrorCode = 'parse' | 'render' | 'download';

function readerErrorMessage(
  code: ReaderErrorCode | null,
  translate: (source: string, english?: string) => string,
): string {
  if (code === 'parse') {
    return translate(
      '电子书解析失败。你可以返回详情页，或下载原始 EPUB 后阅读。',
      'The ebook could not be parsed. Return to its details or download the original EPUB.',
    );
  }
  if (code === 'render') {
    return translate(
      '无法渲染当前章节。你可以返回详情页，或下载原始 EPUB 后阅读。',
      'This chapter could not be rendered. Return to its details or download the original EPUB.',
    );
  }
  if (code === 'download') {
    return translate(
      '电子书下载失败，请稍后重试。',
      'The ebook download failed. Please try again later.',
    );
  }
  return '';
}

interface ReaderPreferences {
  version: 2;
  cfi?: string;
  mode: ReaderMode;
  theme: ReaderTheme;
  fontSize: number;
  lineHeight: ReaderLineHeight;
}

interface TocEntry extends NavItem {
  depth: number;
}

const DEFAULT_PREFERENCES: ReaderPreferences = {
  version: 2,
  mode: 'scrolled',
  theme: 'soft',
  fontSize: 100,
  lineHeight: 'comfortable',
};

const FONT_SIZE_MIN = 80;
const FONT_SIZE_MAX = 160;
const FONT_SIZE_STEP = 5;

const REFLOWABLE_TEXT_SELECTOR = [
  'p', 'li', 'dt', 'dd', 'figcaption', 'caption',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'a', 'span', 'strong', 'em', 'b', 'i', 'u', 'small', 'label', 'summary',
].join(', ');

const LINE_HEIGHT_VALUES: Readonly<Record<ReaderLineHeight, string>> = {
  compact: '1.65',
  comfortable: '1.85',
  relaxed: '2.05',
};


function loadPreferences(storageKey: string): ReaderPreferences {
  try {
    const raw = JSON.parse(localStorage.getItem(storageKey) || '{}') as Partial<ReaderPreferences>;
    return {
      version: 2,
      cfi: typeof raw.cfi === 'string' ? raw.cfi : undefined,
      mode: raw.version === 2 && raw.mode === 'paginated' ? 'paginated' : 'scrolled',
      theme: raw.theme === 'light' || raw.theme === 'dark' ? raw.theme : 'soft',
      fontSize: typeof raw.fontSize === 'number'
        ? Math.min(FONT_SIZE_MAX, Math.max(FONT_SIZE_MIN, Math.round(raw.fontSize / FONT_SIZE_STEP) * FONT_SIZE_STEP))
        : 100,
      lineHeight: raw.lineHeight === 'compact' || raw.lineHeight === 'relaxed'
        ? raw.lineHeight
        : 'comfortable',
    };
  } catch {
    return DEFAULT_PREFERENCES;
  }
}

function normalizeEpubLayout(contents: Contents): void {
  const { document, window: contentWindow } = contents;
  const body = document.body;
  if (!body || !contentWindow) return;

  const normalize = () => {
    if (!document.defaultView) return;
    let changed = false;

    body.querySelectorAll<HTMLElement>('*').forEach((element) => {
      if (
        !element.textContent?.trim()
        || element.matches('script, style, noscript, template')
        || element.closest('svg, math')
      ) return;

      const style = contentWindow.getComputedStyle(element);
      const clipsHorizontally = element.clientWidth > 0
        && element.scrollWidth > element.clientWidth + 1;
      const clipsVertically = element.clientHeight > 0
        && element.scrollHeight > element.clientHeight + 1;
      const fixedTextLayer = style.position === 'absolute' || style.position === 'fixed';
      const constrainedOverflow = (
        (clipsHorizontally && ['hidden', 'clip'].includes(style.overflowX))
        || (clipsVertically && ['hidden', 'clip'].includes(style.overflowY))
      );
      const forcedSingleLine = style.whiteSpace === 'nowrap' || style.textOverflow === 'ellipsis';

      if (forcedSingleLine && !element.classList.contains('ebook-reader-force-wrap')) {
        element.classList.add('ebook-reader-force-wrap');
        changed = true;
      }
      if (
        (fixedTextLayer || constrainedOverflow)
        && !element.classList.contains('ebook-reader-force-reflow')
      ) {
        element.classList.add('ebook-reader-force-reflow');
        changed = true;
      }
    });

    body.querySelectorAll<HTMLElement>('img, svg, picture, video, canvas').forEach((media) => {
      const container = media.closest<HTMLElement>('p, figure, div, section, li');
      if (
        !container
        || container.textContent?.trim()
        || container.classList.contains('ebook-reader-media-container')
      ) return;
      container.classList.add('ebook-reader-media-container');
      changed = true;
    });

    if (changed) contents.emit('expand');
  };

  normalize();
  contentWindow.requestAnimationFrame(normalize);
  void document.fonts?.ready.then(normalize).catch(() => undefined);
}

function flattenToc(items: readonly NavItem[], depth = 0): TocEntry[] {
  return items.flatMap((item) => [
    { ...item, depth },
    ...flattenToc(item.subitems ?? [], depth + 1),
  ]);
}

function waitForBookOpened(book: Book, timeoutMs = 30_000): Promise<void> {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      window.clearTimeout(timeout);
      book.off('openFailed', handleOpenFailure);
    };
    const handleOpenFailure = () => {
      cleanup();
      reject(new Error('EPUB archive could not be opened.'));
    };
    const timeout = window.setTimeout(() => {
      cleanup();
      reject(new Error('EPUB archive opening timed out.'));
    }, timeoutMs);
    book.on('openFailed', handleOpenFailure);
    void book.opened.then(
      () => {
        cleanup();
        resolve();
      },
      handleOpenFailure,
    );
  });
}


function themeRules(theme: ReaderTheme): Record<string, Record<string, string>> {
  const palette = {
    light: { background: '#ffffff', text: '#252525', muted: '#676767', rule: '#e7e7e5' },
    soft: { background: '#f7f2e8', text: '#302d28', muted: '#70695f', rule: '#ded5c7' },
    dark: { background: '#1d1f21', text: '#e5e3df', muted: '#aaa7a1', rule: '#3a3d3f' },
  }[theme];
  return {
    'html, body': {
      'background-color': `${palette.background} !important`,
      color: `${palette.text} !important`,
      margin: '0 !important',
      'box-sizing': 'border-box !important',
    },
    body: {
      'font-family': '"Iowan Old Style", "Noto Serif CJK SC", "Source Han Serif SC", "Songti SC", STSong, Georgia, serif !important',
      'line-height': '1.85 !important',
      'min-width': '0 !important',
      'text-rendering': 'optimizeLegibility',
      '-webkit-font-smoothing': 'antialiased',
      'overflow-wrap': 'break-word',
    },
    'body, body *': {
      'box-sizing': 'border-box !important',
    },
    'div, section, article, main, header, footer, figure, form': {
      'max-width': '100% !important',
    },
    [REFLOWABLE_TEXT_SELECTOR]: {
      color: `${palette.text} !important`,
      'max-width': '100% !important',
      'min-width': '0 !important',
      'overflow-wrap': 'anywhere !important',
      'word-break': 'normal !important',
      'white-space': 'normal !important',
    },
    'p, li, dd': {
      orphans: '3',
      widows: '3',
    },
    p: {
      'text-align': 'justify',
      'text-justify': 'inter-ideograph',
    },
    'h1, h2, h3, h4, h5, h6': {
      'line-height': '1.45 !important',
      'break-after': 'avoid-page',
    },
    a: { color: `${palette.muted} !important` },
    blockquote: {
      color: `${palette.muted} !important`,
      margin: '1.5em 0 !important',
      padding: '.15em 0 .15em 1.25em !important',
      'border-left': `3px solid ${palette.rule} !important`,
    },
    'img, svg': {
      'max-width': '100% !important',
      'max-height': 'calc(100vh - 96px) !important',
      height: 'auto !important',
      'object-fit': 'contain !important',
      'break-inside': 'avoid !important',
      '-webkit-column-break-inside': 'avoid !important',
      'page-break-inside': 'avoid !important',
    },
    '.ebook-reader-media-container': {
      width: 'auto !important',
      'max-width': '100% !important',
      'margin-left': '0 !important',
      'margin-right': '0 !important',
      'padding-left': '0 !important',
      'padding-right': '0 !important',
      'text-indent': '0 !important',
      'break-inside': 'avoid !important',
      '-webkit-column-break-inside': 'avoid !important',
      'page-break-inside': 'avoid !important',
    },
    'ol, ul': {
      'max-width': '100% !important',
      'padding-right': '0 !important',
    },
    table: {
      width: '100% !important',
      'max-width': '100% !important',
      'table-layout': 'fixed !important',
      'border-collapse': 'collapse !important',
    },
    'th, td': {
      'min-width': '0 !important',
      'max-width': '100% !important',
      'overflow-wrap': 'anywhere !important',
      'word-break': 'break-word !important',
    },
    pre: {
      width: '100% !important',
      'max-width': '100% !important',
      'white-space': 'pre-wrap !important',
      'overflow-wrap': 'anywhere !important',
    },
    code: {
      'white-space': 'pre-wrap !important',
      'overflow-wrap': 'anywhere !important',
    },
    '.ebook-reader-force-wrap, .ebook-reader-force-wrap *': {
      'white-space': 'normal !important',
      'text-overflow': 'clip !important',
      'overflow-wrap': 'anywhere !important',
      'word-break': 'break-word !important',
    },
    '.ebook-reader-force-reflow': {
      position: 'static !important',
      inset: 'auto !important',
      width: 'auto !important',
      'min-width': '0 !important',
      'max-width': '100% !important',
      height: 'auto !important',
      'min-height': '0 !important',
      'max-height': 'none !important',
      margin: '0 !important',
      transform: 'none !important',
      overflow: 'visible !important',
    },
  };
}

export default function EbookReaderPage() {
  const { bookId } = useParams();
  const bookEntry = findEbook(bookId);
  const { translate, translateTemplate } = useLanguage();
  const { user } = useAuthStore();
  const identity = user?.id || user?.email.toLocaleLowerCase() || 'unknown';
  const storageKey = useMemo(
    () => `hr-agent:ebook:v1:${identity}:${bookEntry?.id ?? 'missing'}`,
    [bookEntry?.id, identity],
  );
  const initialPreferences = useMemo(() => loadPreferences(storageKey), [storageKey]);
  const [preferences, setPreferences] = useState(initialPreferences);
  const [epubBook, setEpubBook] = useState<Book | null>(null);
  const [toc, setToc] = useState<TocEntry[]>([]);
  const [tocOpen, setTocOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [ready, setReady] = useState(false);
  const [rendering, setRendering] = useState(true);
  const [reflowing, setReflowing] = useState(false);
  const [errorCode, setErrorCode] = useState<ReaderErrorCode | null>(null);
  const error = readerErrorMessage(errorCode, translate);
  const bookMetadata = bookEntry
    ? resourceBookPageContent.books.items.find((item) => item.id === bookEntry.id)
    : undefined;
  const [chapter, setChapter] = useState('');
  const [chapterHref, setChapterHref] = useState('');
  const [progress, setProgress] = useState(0);
  const [locationCfi, setLocationCfi] = useState(initialPreferences.cfi);
  const [downloading, setDownloading] = useState(false);
  const viewerRef = useRef<HTMLDivElement>(null);
  const renditionRef = useRef<Rendition | null>(null);
  const tocRef = useRef<TocEntry[]>([]);
  const currentCfiRef = useRef(initialPreferences.cfi);
  const firstReadyRef = useRef(false);
  const openedAtRef = useRef(performance.now());
  const touchStartRef = useRef<{ x: number; y: number } | null>(null);
  const reflowRequestRef = useRef(0);

  useEffect(() => {
    document.title = bookEntry
      ? translateTemplate('正在阅读 {title}', 'Reading {title}', {
        title: translate(bookEntry.title, bookMetadata?.titleEnglish),
      })
      : translate('未找到书籍', 'Book not found');
  }, [bookEntry, bookMetadata?.titleEnglish, translate, translateTemplate]);

  useEffect(() => {
    const nextPreferences = loadPreferences(storageKey);
    setPreferences(nextPreferences);
    currentCfiRef.current = nextPreferences.cfi;
    setLocationCfi(nextPreferences.cfi);
    firstReadyRef.current = false;
    tocRef.current = [];
    setToc([]);
    setTocOpen(false);
    setSettingsOpen(false);
    setReady(false);
    setReflowing(false);
    setProgress(0);
    setChapter('');
    setChapterHref('');
    setErrorCode(null);
  }, [storageKey]);

  useEffect(() => {
    if (!bookEntry) return undefined;
    let disposed = false;
    let instance: Book | null = null;
    let locationsTimer: number | undefined;
    const controller = new AbortController();
    setEpubBook(null);
    tocRef.current = [];
    setToc([]);
    setRendering(true);
    openedAtRef.current = performance.now();
    void recordEbookEvent(bookEntry.id, 'reader_open').catch(() => undefined);

    void (async () => {
      try {
        instance = ePub(ebookFileUrl(bookEntry.id), {
          openAs: 'epub',
          replacements: 'blobUrl',
          requestMethod: () => loadEbookArchive(bookEntry.id, controller.signal),
        });
        await waitForBookOpened(instance);
        if (disposed) return;
        instance.spine.hooks.serialize.register(sanitizeSerializedEpubSection);
        setEpubBook(instance);
        void instance.loaded.navigation.then((navigation) => {
          if (disposed) return;
          const nextToc = flattenToc(navigation.toc);
          tocRef.current = nextToc;
          setToc(nextToc);
        }).catch(() => {
          if (disposed) return;
          tocRef.current = [];
          setToc([]);
        });
        locationsTimer = window.setTimeout(() => {
          void instance!.locations.generate(1600).then(() => {
            if (disposed || !currentCfiRef.current) return;
            setProgress(Math.round(instance!.locations.percentageFromCfi(currentCfiRef.current) * 100));
          }).catch(() => undefined);
        }, 900);
      } catch (caught) {
        if (disposed || (caught instanceof DOMException && caught.name === 'AbortError')) return;
        setErrorCode('parse');
        setRendering(false);
        void recordEbookEvent(bookEntry.id, 'reader_error').catch(() => undefined);
      }
    })();

    return () => {
      disposed = true;
      if (locationsTimer !== undefined) window.clearTimeout(locationsTimer);
      controller.abort();
      instance?.destroy();
    };
  }, [bookEntry]);

  useEffect(() => {
    if (!epubBook || !viewerRef.current || !bookEntry) return undefined;
    let disposed = false;
    setRendering(true);
    setReady(false);
    setReflowing(false);
    setErrorCode(null);
    const renditionOptions = {
      width: '100%',
      height: '100%',
      manager: preferences.mode === 'scrolled' ? 'continuous' : 'default',
      flow: preferences.mode === 'scrolled' ? 'scrolled-doc' : 'paginated',
      spread: 'none',
      allowScriptedContent: false,
    };
    const rendition = epubBook.renderTo(viewerRef.current, renditionOptions);
    renditionRef.current = rendition;
    rendition.hooks.content.register(sanitizeEpubContents);
    rendition.hooks.content.register(normalizeEpubLayout);
    rendition.themes.register('reader', themeRules(preferences.theme));
    rendition.themes.select('reader');
    rendition.themes.fontSize(`${preferences.fontSize}%`);
    rendition.themes.override('line-height', LINE_HEIGHT_VALUES[preferences.lineHeight], true);

    const handleRelocated = (location: Location) => {
      if (disposed) return;
      const cfi = location.start.cfi;
      currentCfiRef.current = cfi;
      const percentage = epubBook.locations.length() > 0
        ? epubBook.locations.percentageFromCfi(cfi)
        : location.start.percentage || 0;
      setProgress(Math.round(Math.max(0, Math.min(1, percentage)) * 100));
      setLocationCfi(cfi);
      const currentHref = location.start.href.split('#')[0];
      setChapterHref(currentHref);
      const currentChapter = tocRef.current.find(({ href }) => href.split('#')[0] === currentHref);
      setChapter(currentChapter?.label?.trim() || bookEntry.title);
    };
    rendition.on('relocated', handleRelocated);

    void rendition.display(currentCfiRef.current).then(() => {
      if (disposed) return;
      setRendering(false);
      setReady(true);
      if (!firstReadyRef.current) {
        firstReadyRef.current = true;
        void recordEbookEvent(
          bookEntry.id,
          'reader_ready',
          performance.now() - openedAtRef.current,
        ).catch(() => undefined);
        if (initialPreferences.cfi) {
          void recordEbookEvent(bookEntry.id, 'progress_restored').catch(() => undefined);
        }
      }
    }).catch(() => {
      if (disposed) return;
      setErrorCode('render');
      setRendering(false);
      void recordEbookEvent(bookEntry.id, 'reader_error').catch(() => undefined);
    });

    return () => {
      disposed = true;
      reflowRequestRef.current += 1;
      rendition.off('relocated', handleRelocated);
      if (renditionRef.current === rendition) renditionRef.current = null;
      rendition.destroy();
      if (viewerRef.current) viewerRef.current.replaceChildren();
    };
  }, [bookEntry, epubBook, initialPreferences.cfi, preferences.mode]);

  useEffect(() => {
    const rendition = renditionRef.current;
    if (!rendition) return;
    rendition.themes.register('reader', themeRules(preferences.theme));
    rendition.themes.select('reader');
  }, [preferences.theme]);

  useEffect(() => {
    if (!bookEntry || !chapterHref) return;
    const currentChapter = toc.find(({ href }) => href.split('#')[0] === chapterHref);
    if (currentChapter?.label?.trim()) setChapter(currentChapter.label.trim());
  }, [bookEntry, chapterHref, toc]);

  useEffect(() => {
    const rendition = renditionRef.current;
    if (!rendition || !ready) return undefined;
    const requestId = ++reflowRequestRef.current;
    const anchor = currentCfiRef.current;
    let cancelled = false;
    const timeout = window.setTimeout(() => {
      if (cancelled || renditionRef.current !== rendition) return;
      setReflowing(true);
      rendition.themes.fontSize(`${preferences.fontSize}%`);
      rendition.themes.override('line-height', LINE_HEIGHT_VALUES[preferences.lineHeight], true);

      void new Promise<void>((resolve) => {
        window.requestAnimationFrame(() => window.requestAnimationFrame(() => resolve()));
      }).then(async () => {
        if (cancelled || requestId !== reflowRequestRef.current || renditionRef.current !== rendition) return;
        if (anchor) await rendition.display(anchor);
        await rendition.reportLocation();
      }).catch(() => undefined).finally(() => {
        if (!cancelled && requestId === reflowRequestRef.current) setReflowing(false);
      });
    }, 90);

    return () => {
      cancelled = true;
      window.clearTimeout(timeout);
    };
  }, [preferences.fontSize, preferences.lineHeight, ready]);

  useEffect(() => {
    if (!tocOpen && !settingsOpen) return undefined;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setTocOpen(false);
      setSettingsOpen(false);
    };
    window.addEventListener('keydown', handleEscape);
    return () => window.removeEventListener('keydown', handleEscape);
  }, [settingsOpen, tocOpen]);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      const next: ReaderPreferences = {
        ...preferences,
        version: 2,
        cfi: locationCfi,
      };
      localStorage.setItem(storageKey, JSON.stringify(next));
    }, 300);
    return () => window.clearTimeout(timeout);
  }, [locationCfi, preferences, storageKey]);

  const move = useCallback(async (direction: -1 | 1) => {
    const rendition = renditionRef.current;
    if (!rendition) return;
    const startedAt = performance.now();
    if (direction < 0) await rendition.prev();
    else await rendition.next();
    if (bookEntry) {
      void recordEbookEvent(
        bookEntry.id,
        'page_change',
        performance.now() - startedAt,
      ).catch(() => undefined);
    }
  }, [bookEntry]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest('input, textarea, select, button, a')) return;
      if (event.key === 'ArrowLeft' && preferences.mode === 'paginated') {
        event.preventDefault();
        void move(-1);
      } else if (event.key === 'ArrowRight' && preferences.mode === 'paginated') {
        event.preventDefault();
        void move(1);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [move, preferences.mode]);

  const selectChapter = (href: string) => {
    const rendition = renditionRef.current;
    if (!rendition) return;
    setTocOpen(false);
    setSettingsOpen(false);
    void rendition.display(href);
  };

  const handleDownload = async () => {
    if (!bookEntry) return;
    setDownloading(true);
    try {
      const artifact = await downloadEbook(bookEntry.id);
      const url = URL.createObjectURL(artifact.blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = artifact.filename;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setErrorCode('download');
    } finally {
      setDownloading(false);
    }
  };

  if (!bookEntry) {
    return (
      <main className="ebook-reader-page ebook-reader-missing">
        <BookOpen aria-hidden="true" />
        <h1>{translate('未找到这本电子书', 'This ebook could not be found')}</h1>
        <Link to="/resources">{translate('返回资源中心', 'Back to Resources')}</Link>
      </main>
    );
  }

  const adjustFont = (delta: number) => {
    setPreferences((current) => ({
      ...current,
      fontSize: Math.min(FONT_SIZE_MAX, Math.max(FONT_SIZE_MIN, current.fontSize + delta)),
    }));
  };

  return (
    <main className={`ebook-reader-page ebook-reader-theme-${preferences.theme}`}>
      <header className="ebook-reader-header">
        <div className="ebook-reader-book">
          <Link to={ebookDetailPath(bookEntry.id)} aria-label={translate('返回书籍详情', 'Back to book details')}>
            <ArrowLeft aria-hidden="true" />
          </Link>
          <strong>{bookEntry.title}</strong>
        </div>
        <p className="ebook-reader-current-chapter">{chapter || translate('正在准备章节', 'Preparing chapter')}</p>
        <div className="ebook-reader-header-actions">
          <button
            type="button"
            onClick={() => {
              setSettingsOpen(false);
              setTocOpen((open) => !open);
            }}
            aria-expanded={tocOpen}
          >
            <List aria-hidden="true" /><span>{translate('目录', 'Contents')}</span>
          </button>
          <button
            type="button"
            onClick={() => {
              setTocOpen(false);
              setSettingsOpen((open) => !open);
            }}
            aria-expanded={settingsOpen}
            aria-controls="ebook-reader-settings"
          >
            <Settings2 aria-hidden="true" /><span>{translate('设置', 'Settings')}</span>
          </button>
          <button type="button" onClick={() => void handleDownload()} disabled={downloading}>
            {downloading ? <LoaderCircle className="spin" aria-hidden="true" /> : <Download aria-hidden="true" />}
            <span>{translate('下载', 'Download')}</span>
          </button>
        </div>
      </header>

      {settingsOpen && (
        <>
          <button
            className="ebook-reader-settings-backdrop"
            type="button"
            onClick={() => setSettingsOpen(false)}
            aria-label={translate('关闭阅读设置', 'Close reading settings')}
          />
          <aside id="ebook-reader-settings" className="ebook-reader-settings" aria-labelledby="ebook-reader-settings-title">
            <header>
              <strong id="ebook-reader-settings-title">{translate('阅读设置', 'Reading settings')}</strong>
              <button type="button" onClick={() => setSettingsOpen(false)} aria-label={translate('关闭阅读设置', 'Close reading settings')}><X aria-hidden="true" /></button>
            </header>
            <section>
              <div className="ebook-reader-setting-label"><Type aria-hidden="true" /><span>{translate('字号', 'Font size')}</span><output>{preferences.fontSize}%</output></div>
              <div className="ebook-reader-font-controls">
                <button type="button" onClick={() => adjustFont(-FONT_SIZE_STEP)} disabled={preferences.fontSize <= FONT_SIZE_MIN} aria-label={translate('减小字号', 'Decrease font size')}><Minus aria-hidden="true" /></button>
                <input
                  type="range"
                  min={FONT_SIZE_MIN}
                  max={FONT_SIZE_MAX}
                  step={FONT_SIZE_STEP}
                  value={preferences.fontSize}
                  onChange={(event) => setPreferences((current) => ({ ...current, fontSize: Number(event.target.value) }))}
                  aria-label={translate('字号比例', 'Font-size percentage')}
                />
                <button type="button" onClick={() => adjustFont(FONT_SIZE_STEP)} disabled={preferences.fontSize >= FONT_SIZE_MAX} aria-label={translate('增大字号', 'Increase font size')}><Plus aria-hidden="true" /></button>
              </div>
            </section>
            <section>
              <div className="ebook-reader-setting-label"><Rows3 aria-hidden="true" /><span>{translate('行距', 'Line spacing')}</span></div>
              <div className="ebook-reader-segmented" role="group" aria-label={translate('正文行距', 'Body line spacing')}>
                {(['compact', 'comfortable', 'relaxed'] as const).map((lineHeight) => (
                  <button
                    type="button"
                    className={preferences.lineHeight === lineHeight ? 'is-active' : ''}
                    onClick={() => setPreferences((current) => ({ ...current, lineHeight }))}
                    key={lineHeight}
                  >{translate(
                    { compact: '紧凑', comfortable: '舒适', relaxed: '宽松' }[lineHeight],
                    { compact: 'Compact', comfortable: 'Comfortable', relaxed: 'Relaxed' }[lineHeight],
                  )}</button>
                ))}
              </div>
            </section>
            <section>
              <div className="ebook-reader-setting-label"><BookOpen aria-hidden="true" /><span>{translate('阅读方式', 'Reading mode')}</span></div>
              <div className="ebook-reader-segmented" role="group" aria-label={translate('阅读模式', 'Reading mode')}>
                <button type="button" className={preferences.mode === 'paginated' ? 'is-active' : ''} onClick={() => setPreferences((current) => ({ ...current, mode: 'paginated' }))}><BookOpen aria-hidden="true" />{translate('分页', 'Paginated')}</button>
                <button type="button" className={preferences.mode === 'scrolled' ? 'is-active' : ''} onClick={() => setPreferences((current) => ({ ...current, mode: 'scrolled' }))}><Rows3 aria-hidden="true" />{translate('滚动', 'Scroll')}</button>
              </div>
            </section>
            <section>
              <div className="ebook-reader-setting-label"><span>{translate('阅读背景', 'Reading background')}</span></div>
              <div className="ebook-reader-themes" role="group" aria-label={translate('阅读主题', 'Reading theme')}>
                {(['light', 'soft', 'dark'] as const).map((theme) => (
                  <button
                    type="button"
                    className={preferences.theme === theme ? 'is-active' : ''}
                    onClick={() => setPreferences((current) => ({ ...current, theme }))}
                    aria-label={translate(
                      { light: '浅色主题', soft: '柔和主题', dark: '深色主题' }[theme],
                      { light: 'Light theme', soft: 'Soft theme', dark: 'Dark theme' }[theme],
                    )}
                    key={theme}
                  >
                    <span className={`ebook-theme-swatch ebook-theme-swatch-${theme}`} />
                    <span>{translate(
                      { light: '明亮', soft: '柔和', dark: '深色' }[theme],
                      { light: 'Light', soft: 'Soft', dark: 'Dark' }[theme],
                    )}</span>
                  </button>
                ))}
              </div>
            </section>
          </aside>
        </>
      )}

      <div className="ebook-reader-workspace">
        <aside className={tocOpen ? 'ebook-reader-toc is-open' : 'ebook-reader-toc'} aria-hidden={!tocOpen}>
          <header><span><strong>{translate('目录', 'Contents')}</strong><small>{translateTemplate('{count} 个章节', '{count} chapters', { count: toc.length })}</small></span><button type="button" onClick={() => setTocOpen(false)} aria-label={translate('关闭目录', 'Close contents')}><X aria-hidden="true" /></button></header>
          <nav aria-label={translate('电子书目录', 'Ebook contents')}>
            {toc.length === 0 && <p>{translate('目录正在加载', 'Loading contents')}</p>}
            {toc.map((item) => {
              const itemHref = item.href.split('#')[0];
              const active = itemHref === chapterHref;
              return (
              <button
                type="button"
                key={`${item.id}-${item.href}`}
                className={active ? 'is-active' : ''}
                style={{ '--toc-depth': item.depth } as React.CSSProperties}
                onClick={() => selectChapter(item.href)}
                aria-current={active ? 'location' : undefined}
              >{item.label}</button>
              );
            })}
          </nav>
        </aside>
        {tocOpen && <button className="ebook-reader-toc-backdrop" type="button" onClick={() => setTocOpen(false)} aria-label={translate('关闭目录', 'Close contents')} />}

        <section
          className="ebook-reader-stage"
          onTouchStart={(event) => {
            const touch = event.changedTouches[0];
            touchStartRef.current = { x: touch.clientX, y: touch.clientY };
          }}
          onTouchEnd={(event) => {
            if (preferences.mode !== 'paginated' || !touchStartRef.current) return;
            const touch = event.changedTouches[0];
            const deltaX = touch.clientX - touchStartRef.current.x;
            const deltaY = touch.clientY - touchStartRef.current.y;
            touchStartRef.current = null;
            if (Math.abs(deltaX) > 60 && Math.abs(deltaX) > Math.abs(deltaY) * 1.2) {
              void move(deltaX > 0 ? -1 : 1);
            }
          }}
        >
          <div className="ebook-reader-paper">
            {(rendering || !ready) && !error && (
              <div className="ebook-reader-loading" role="status">
                <span>{translate('正在打开电子书', 'Opening ebook')}</span>
              </div>
            )}
            {error && (
              <div className="ebook-reader-error" role="alert">
                <BookOpen aria-hidden="true" />
                <strong>{translate('暂时无法继续阅读', 'Reading is temporarily unavailable')}</strong>
                <p>{error}</p>
                <div><Link to={ebookDetailPath(bookEntry.id)}>{translate('返回详情', 'Back to details')}</Link><button type="button" onClick={() => void handleDownload()}>{translate('下载 EPUB', 'Download EPUB')}</button></div>
              </div>
            )}
            <div className="ebook-reader-viewer" ref={viewerRef} />
          </div>
          {preferences.mode === 'paginated' && ready && !error && (
            <>
              <button className="ebook-reader-page-button is-previous" type="button" onClick={() => void move(-1)} aria-label={translate('上一页', 'Previous page')}><ChevronLeft aria-hidden="true" /></button>
              <button className="ebook-reader-page-button is-next" type="button" onClick={() => void move(1)} aria-label={translate('下一页', 'Next page')}><ChevronRight aria-hidden="true" /></button>
            </>
          )}
        </section>
      </div>
      <footer className="ebook-reader-footer">
        <div className="ebook-reader-progress-track" role="progressbar" aria-label={translate('阅读进度', 'Reading progress')} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}>
          <i style={{ '--ebook-reader-progress': progress / 100 } as React.CSSProperties} />
        </div>
        <span className="ebook-reader-footer-status">{reflowing ? translate('正在重新排版', 'Reflowing') : `${progress}%`}</span>
      </footer>
    </main>
  );
}

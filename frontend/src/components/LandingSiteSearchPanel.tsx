import { Search, X } from 'lucide-react';
import {
  type FormEvent,
  type KeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useNavigate } from 'react-router-dom';
import { homeExperienceContent } from '../content/home-experience-content';
import { performanceManagementDimensions } from '../content/performance-management-content';
import { resourceBookPageContent } from '../content/resource-book-content';
import { toolkitPageList } from '../content/toolkit-content';
import { useLanguage } from '../i18n/LanguageContext';

interface LandingSiteSearchPanelProps {
  onClose: () => void;
}

interface SiteSearchItem {
  id: string;
  title: string;
  titleEnglish?: string;
  summary: string;
  summaryEnglish?: string;
  typeLabel: string;
  typeLabelEnglish: string;
  keywords: readonly string[];
  href: string;
  priority?: number;
}

const coreSearchItems: readonly SiteSearchItem[] = [
  {
    id: 'page-home',
    title: '首页',
    summary: homeExperienceContent.hero.description,
    typeLabel: '页面',
    typeLabelEnglish: 'Page',
    keywords: ['主页', '人才管理', '领导力', 'Performance Feedback'],
    href: '/',
  },
  {
    id: 'page-workspace',
    title: '管理沟通工作台',
    summary: '结合员工档案、沟通意图、谈前指导与多轮预演，准备下一次管理对话。',
    typeLabel: '工作平台',
    typeLabelEnglish: 'Workspace',
    keywords: ['员工', '沟通', '预演', '反馈', 'Agent'],
    href: '/app/introduction',
  },
  {
    id: 'page-solutions',
    title: '解决方案',
    summary: '从战略分析、教练目标、反馈评估到发展框架，查找可直接应用的方法。',
    typeLabel: '页面',
    typeLabelEnglish: 'Page',
    keywords: ['解决方案', '方法', '框架', '工具'],
    href: '/solutions',
  },
  {
    id: 'page-resources',
    title: '资源中心',
    summary: '按概述、目标设定、绩效反馈与评估、结果应用和低绩效管理五个维度浏览内容。',
    summaryEnglish: 'Browse content across five dimensions: overview, goal setting, performance feedback and review, consequence management, and under-performance management.',
    typeLabel: '页面',
    typeLabelEnglish: 'Page',
    keywords: [
      '资源中心',
      '绩效管理',
      ...performanceManagementDimensions.flatMap((dimension) => [
        dimension.title,
        dimension.englishTitle,
      ]),
    ],
    href: '/resources',
  },
];

const performanceManagementSearchItems: readonly SiteSearchItem[] = performanceManagementDimensions.map(
  (dimension) => {
    const module = homeExperienceContent.resourceModules.find((item) => item.id === dimension.slug);
    return {
      id: `performance-management-${dimension.slug}`,
      title: dimension.title,
      titleEnglish: dimension.englishTitle,
      summary: dimension.summary,
      summaryEnglish: module?.descriptionEnglish,
      typeLabel: '绩效管理维度',
      typeLabelEnglish: 'Performance management dimension',
      keywords: [
        dimension.number,
        dimension.title,
        dimension.englishTitle,
        dimension.tagline,
        dimension.objective,
        ...dimension.keywords,
      ],
      href: dimension.path,
      priority: 80,
    };
  },
);

const toolkitSearchAliases: Readonly<Record<string, readonly string[]>> = {
  'strategy-analysis': ['战略分析', '业务分析', '决策分析', '组织战略'],
  'coaching-goals': ['目标设定', '目标管理', '绩效目标', '教练', '行动计划'],
  'feedback-assessment': ['绩效反馈', '绩效评估', '反馈评估', '人才评鉴', '复盘'],
  'development-frameworks': ['人才发展', '发展计划', '能力发展', '领导力发展', '继任'],
};

const toolkitSearchItems: readonly SiteSearchItem[] = toolkitPageList.flatMap((page) => [
  {
    id: `solution-${page.slug}`,
    title: page.title,
    titleEnglish: page.titleEnglish,
    summary: page.summary,
    summaryEnglish: page.summaryEnglish,
    typeLabel: '解决方案',
    typeLabelEnglish: 'Solution',
    keywords: [
      page.navLabel,
      page.navLabelEnglish,
      page.introTitle,
      ...page.introParagraphs,
      ...(toolkitSearchAliases[page.slug] ?? []),
    ],
    href: page.path,
  },
  ...page.tools.map((tool) => ({
    id: `tool-${page.slug}-${tool.id}`,
    title: tool.title,
    summary: tool.summary,
    summaryEnglish: tool.summaryEnglish,
    typeLabel: '管理工具',
    typeLabelEnglish: 'Management tool',
    keywords: [
      page.title,
      page.titleEnglish,
      page.navLabel,
      page.navLabelEnglish,
      tool.application,
      ...(toolkitSearchAliases[page.slug] ?? []),
    ],
    href: tool.path,
  })),
]);

const resourceSearchItems: readonly SiteSearchItem[] = [
  ...resourceBookPageContent.books.items.map((item) => ({
    id: `book-${item.id}`,
    title: item.title,
    titleEnglish: item.titleEnglish,
    summary: item.summary,
    summaryEnglish: item.summaryEnglish,
    typeLabel: '延伸阅读',
    typeLabelEnglish: 'Further reading',
    keywords: [item.authors, ...item.tags],
    href: item.href,
  })),
];

const siteSearchItems = [
  ...coreSearchItems,
  ...performanceManagementSearchItems,
  ...toolkitSearchItems,
  ...resourceSearchItems,
] as const;

function scoreSearchItem(item: SiteSearchItem, query: string, terms: readonly string[]) {
  const title = item.title.toLocaleLowerCase();
  const summary = item.summary.toLocaleLowerCase();
  const typeLabel = item.typeLabel.toLocaleLowerCase();
  const keywords = item.keywords.join(' ').toLocaleLowerCase();
  let score = 0;

  for (const term of terms) {
    let termScore = 0;
    if (title === term) termScore += 90;
    else if (title.startsWith(term)) termScore += 64;
    else if (title.includes(term)) termScore += 48;
    if (keywords.includes(term)) termScore += 24;
    if (typeLabel.includes(term)) termScore += 16;
    if (summary.includes(term)) termScore += 10;
    if (termScore === 0) return -1;
    score += termScore;
  }

  if (title.includes(query)) score += 28;
  score += item.priority ?? 0;
  return score;
}

export default function LandingSiteSearchPanel({ onClose }: LandingSiteSearchPanelProps) {
  const navigate = useNavigate();
  const { translate } = useLanguage();
  const surfaceRef = useRef<HTMLElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const localizedSearchItems = useMemo(() => siteSearchItems.map((item) => {
    const title = translate(item.title, item.titleEnglish);
    const summary = translate(item.summary, item.summaryEnglish);
    const typeLabel = translate(item.typeLabel, item.typeLabelEnglish);
    return {
      ...item,
      title,
      summary,
      typeLabel,
      keywords: [
        ...item.keywords,
        item.titleEnglish ?? '',
        item.summaryEnglish ?? '',
        item.typeLabelEnglish,
        title,
        summary,
        typeLabel,
      ].filter(Boolean),
    };
  }), [translate]);
  const results = useMemo(() => {
    if (!normalizedQuery) return [];
    const queryTerms = normalizedQuery.split(/\s+/).filter(Boolean);
    return localizedSearchItems
      .map((item) => ({ item, score: scoreSearchItem(item, normalizedQuery, queryTerms) }))
      .filter(({ score }) => score >= 0)
      .sort((left, right) => right.score - left.score)
      .slice(0, 10)
      .map(({ item }) => item);
  }, [localizedSearchItems, normalizedQuery]);

  useEffect(() => {
    const focusTimer = window.setTimeout(() => inputRef.current?.focus(), 100);
    return () => window.clearTimeout(focusTimer);
  }, []);

  useEffect(() => {
    setActiveIndex(0);
  }, [normalizedQuery]);

  const activateResult = (item: SiteSearchItem) => {
    if (/^https?:\/\//i.test(item.href)) {
      window.open(item.href, '_blank', 'noopener,noreferrer');
    } else {
      navigate(item.href);
    }
    onClose();
  };

  const submitSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const selectedResult = results[activeIndex] ?? results[0];
    if (selectedResult) activateResult(selectedResult);
  };

  const handleSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (!results.length) return;
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActiveIndex((index) => (index + 1) % results.length);
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActiveIndex((index) => (index - 1 + results.length) % results.length);
    }
  };

  const keepFocusInSearch = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Tab') return;
    const focusable = Array.from(
      surfaceRef.current?.querySelectorAll<HTMLElement>(
        'input, button:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
      ) ?? [],
    ).filter((element) => element.getClientRects().length > 0);
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <section
      ref={surfaceRef}
      id="landing-site-search"
      className="landing-site-search-surface is-open"
      role="dialog"
      aria-modal="true"
      aria-label={translate('全站搜索')}
      onKeyDown={keepFocusInSearch}
    >
      <div
        className="hamburger-curtain"
        aria-hidden="true"
        onClick={onClose}
      />

      <div className="landing-site-search-bar">
        <form className="landing-site-search-form" role="search" onSubmit={submitSearch}>
          <input
            ref={inputRef}
            role="combobox"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={handleSearchKeyDown}
            placeholder={translate('请输入关键词...')}
            aria-label={translate('搜索网站内容')}
            title={translate('搜索')}
            aria-autocomplete="list"
            aria-expanded={Boolean(normalizedQuery)}
            aria-controls="landing-site-search-results"
            aria-activedescendant={results[activeIndex] ? `landing-search-result-${results[activeIndex].id}` : undefined}
            autoComplete="off"
          />
          <button
            className="landing-site-search-close"
            type="button"
            onClick={onClose}
            aria-label={translate('关闭搜索')}
            title={translate('关闭搜索')}
          >
            <X aria-hidden="true" />
          </button>
          <button
            className="landing-site-search-submit"
            type="submit"
            aria-label={translate('搜索')}
            title={translate('搜索')}
          >
            <Search aria-hidden="true" />
          </button>
        </form>
      </div>

      <div
        className={`landing-site-search-dropdown${normalizedQuery ? ' is-visible' : ''}`}
        aria-hidden={!normalizedQuery}
      >
        <div className="landing-site-search-dropdown-inner">
          {normalizedQuery && (
            <div id="landing-site-search-results" className="landing-site-search-results" aria-live="polite">
              {results.length > 0 ? (
                <div role="listbox" aria-label={translate('搜索建议')}>
                  {results.map((item, index) => (
                    <button
                      id={`landing-search-result-${item.id}`}
                      className={`landing-site-search-result${activeIndex === index ? ' is-active' : ''}`}
                      type="button"
                      role="option"
                      aria-selected={activeIndex === index}
                      aria-posinset={index + 1}
                      aria-setsize={results.length}
                      key={item.id}
                      onMouseEnter={() => setActiveIndex(index)}
                      onFocus={() => setActiveIndex(index)}
                      onClick={() => activateResult(item)}
                    >
                      <span>{item.typeLabel} · {item.title}</span>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="landing-site-search-empty">{translate('没有找到匹配内容，请尝试其他关键词。')}</p>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

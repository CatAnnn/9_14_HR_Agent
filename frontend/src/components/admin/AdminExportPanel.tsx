import {
  AlertTriangle,
  ArrowRight,
  ArrowUpDown,
  CalendarDays,
  ChevronDown,
  Download,
  Eye,
  History,
  ListFilter,
  LoaderCircle,
  RefreshCw,
  Search,
  UsersRound,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { authApi } from '../../api/auth';
import { useDialogFocus } from '../../hooks/useDialogFocus';
import { intentName } from '../../i18n/businessLabels';
import { useLanguage, type AppLanguage } from '../../i18n/LanguageContext';
import { useAuthStore } from '../../store/authStore';
import type { AdminExportConversation, AdminUsageSessionDetail } from '../../types/auth';
import {
  DEFAULT_ADMIN_USAGE_FILTERS,
  adminUsageFilterSearch,
  hasAdminUsageFilters,
  readAdminUsageFilters,
  writeAdminUsageFilters,
  type AdminUsageFilters,
} from '../../utils/adminUsageFilters';
import {
  adminUsageSessionPath,
  defaultAdminUsageSection,
} from '../../utils/adminUsageSession';
import { userFacingErrorMessage } from '../../utils/displayText';
import { AdminUsageSessionDialog } from './AdminUsageSessionDialog';

interface AdminExportPanelProps {
  busy: boolean;
}

interface PendingExport {
  sessionIds: string[];
  exportKey: string;
  userCount: number;
  hiddenSelectionCount: number;
}

type Translate = (source: string, english?: string) => string;
type AdminExportFeedbackError =
  | { kind: 'validation'; code: 'selection_required' | 'invalid_date_range' }
  | { kind: 'request'; cause: unknown };
interface AdminExportFeedbackMessage {
  kind: 'export_created';
  filename: string;
}

function translatedTemplate(
  translate: Translate,
  source: string,
  english: string,
  values: Readonly<Record<string, string | number>>,
): string {
  return translate(source, english).replace(/\{([a-z_]+)\}/giu, (placeholder, key: string) => (
    Object.prototype.hasOwnProperty.call(values, key) ? String(values[key]) : placeholder
  ));
}

function adminExportErrorMessage(
  translate: Translate,
  feedback: AdminExportFeedbackError | null,
): string {
  if (!feedback) return '';
  if (feedback.kind === 'request') {
    return userFacingErrorMessage(
      feedback.cause,
      undefined,
      translate('使用记录导出失败。', 'The usage-record export failed.'),
    );
  }
  return feedback.code === 'selection_required'
    ? translate(
      '请至少选择一次需要导出的对话。',
      'Select at least one conversation to export.',
    )
    : translate(
      '结束日期不能早于开始日期。',
      'The end date cannot be earlier than the start date.',
    );
}

function adminExportMessage(
  translate: Translate,
  feedback: AdminExportFeedbackMessage | null,
): string {
  if (!feedback) return '';
  return translatedTemplate(
    translate,
    '导出已生成：{filename}',
    'Export created: {filename}',
    { filename: feedback.filename },
  );
}

const ADMIN_USAGE_SELECTION_STORAGE_PREFIX = 'hr-agent-admin-usage-selection:';
const ADMIN_USAGE_SCROLL_STORAGE_PREFIX = 'hr-agent-admin-usage-scroll:';
const ADMIN_FILTERS_MOBILE_QUERY = '(max-width: 720px)';

function selectionStorageKey(email: string | null | undefined): string {
  const normalized = String(email || '').trim().toLowerCase();
  return normalized ? `${ADMIN_USAGE_SELECTION_STORAGE_PREFIX}${normalized}` : '';
}

function scrollStorageKeyFor(
  email: string | null | undefined,
  filterSearch: string,
): string {
  const normalized = String(email || '').trim().toLowerCase();
  return normalized
    ? `${ADMIN_USAGE_SCROLL_STORAGE_PREFIX}${normalized}:${filterSearch}`
    : '';
}

function readStoredSelection(key: string): string[] {
  if (!key || typeof window === 'undefined') return [];
  try {
    const parsed: unknown = JSON.parse(window.sessionStorage.getItem(key) || '[]');
    if (!Array.isArray(parsed)) return [];
    return Array.from(new Set(
      parsed
        .filter((value): value is string => typeof value === 'string')
        .map((value) => value.trim())
        .filter(Boolean),
    ));
  } catch {
    return [];
  }
}

function filtersExpandedInitially(): boolean {
  return typeof window === 'undefined'
    || typeof window.matchMedia !== 'function'
    || !window.matchMedia(ADMIN_FILTERS_MOBILE_QUERY).matches;
}

const INTENT_NAMES_CHINESE: Readonly<Record<string, string>> = {
  development: '发展型反馈',
  improvement: '改进型反馈',
  exit: '退出型沟通',
  development_improvement: '发展与改进混合反馈',
  improvement_exit: '改进与退出预警',
};
const INTENT_SEARCH_LANGUAGES: readonly AppLanguage[] = ['zh-CN', 'en', 'de', 'ja'];

function localizedIntentName(
  id: string | null | undefined,
  language: AppLanguage,
): string {
  const normalizedId = String(id || '').trim();
  return intentName(
    normalizedId,
    INTENT_NAMES_CHINESE[normalizedId] || normalizedId,
    language,
  );
}

function localizedIntentSearchNames(id: string | null | undefined): string[] {
  return INTENT_SEARCH_LANGUAGES.map((language) => localizedIntentName(id, language));
}

const BEIJING_DATE_TIME: Record<AppLanguage, Intl.DateTimeFormat> = {
  'zh-CN': new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }),
  en: new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }),
  de: new Intl.DateTimeFormat('de-DE', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }),
  ja: new Intl.DateTimeFormat('ja-JP', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }),
};

const BEIJING_DATE_KEY = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});

function validDate(value?: string | null): Date | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function conversationActivityDate(conversation: AdminExportConversation): Date | null {
  return validDate(conversation.conversation_started_at)
    || validDate(conversation.session_created_at);
}

function conversationDateKey(conversation: AdminExportConversation): string | null {
  const parsed = conversationActivityDate(conversation);
  if (!parsed) return null;
  const parts = Object.fromEntries(
    BEIJING_DATE_KEY.formatToParts(parsed).map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function conversationDateLabel(
  conversation: AdminExportConversation,
  language: AppLanguage,
  translate: Translate,
): string {
  const startedAt = validDate(conversation.conversation_started_at);
  const parsed = startedAt || validDate(conversation.session_created_at);
  if (!parsed) return translate('日期未记录', 'Date not recorded');
  const formatted = BEIJING_DATE_TIME[language].format(parsed);
  return startedAt
    ? formatted
    : translatedTemplate(
      translate,
      '创建于 {date}',
      'Created {date}',
      { date: formatted },
    );
}

function userLabel(conversation: AdminExportConversation): string {
  return conversation.user_display_name
    || conversation.user_email.split('@')[0]
    || conversation.user_email;
}

function rehearsalStatus(conversation: AdminExportConversation, translate: Translate): string {
  if (conversation.has_rehearsal) {
    return translate('已进入多轮预演', 'Multi-turn rehearsal started');
  }
  if (conversation.has_conversation) {
    return translate('预演未完成', 'Rehearsal incomplete');
  }
  return translate('未进入预演', 'Rehearsal not started');
}

function saveDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function AdminExportPanel({ busy }: AdminExportPanelProps) {
  const { language, translate } = useLanguage();
  const { user } = useAuthStore();
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = useMemo(() => readAdminUsageFilters(searchParams), [searchParams]);
  const {
    query: search,
    userEmail: selectedUserEmail,
    startDate,
    endDate,
    sortOrder,
    scope,
  } = filters;
  const storedSelectionKey = useMemo(
    () => selectionStorageKey(user?.email),
    [user?.email],
  );
  const [conversations, setConversations] = useState<AdminExportConversation[]>([]);
  const [selectedSessionIds, setSelectedSessionIds] = useState<string[]>(
    () => readStoredSelection(storedSelectionKey),
  );
  const [catalogLoaded, setCatalogLoaded] = useState(false);
  const [loadingConversations, setLoadingConversations] = useState(true);
  const [catalogError, setCatalogError] = useState<unknown | null>(null);
  const [activeExportKey, setActiveExportKey] = useState<string | null>(null);
  const [pendingExport, setPendingExport] = useState<PendingExport | null>(null);
  const [detailConversation, setDetailConversation] = useState<AdminExportConversation | null>(null);
  const [usageDetail, setUsageDetail] = useState<AdminUsageSessionDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<unknown | null>(null);
  const [filtersExpanded, setFiltersExpanded] = useState(filtersExpandedInitially);
  const [actionError, setActionError] = useState<AdminExportFeedbackError | null>(null);
  const [actionMessage, setActionMessage] = useState<AdminExportFeedbackMessage | null>(null);
  const error = adminExportErrorMessage(translate, actionError);
  const message = adminExportMessage(translate, actionMessage);
  const visibleSelectAllRef = useRef<HTMLInputElement>(null);
  const exportCancelButtonRef = useRef<HTMLButtonElement>(null);
  const exportHeadingRef = useRef<HTMLHeadingElement>(null);
  const selectedStorageKeyRef = useRef(storedSelectionKey);
  const catalogRequestRef = useRef(0);
  const detailRequestRef = useRef(0);

  const updateFilters = useCallback((patch: Partial<AdminUsageFilters>) => {
    const nextFilters = { ...filters, ...patch };
    const nextSearchParams = writeAdminUsageFilters(searchParams, nextFilters);
    const currentWindowTop = window.scrollY;
    const nextScrollStorageKey = scrollStorageKeyFor(
      user?.email,
      adminUsageFilterSearch(nextSearchParams),
    );
    try {
      if (nextScrollStorageKey) {
        window.sessionStorage.setItem(`${nextScrollStorageKey}:window`, String(currentWindowTop));
      }
    } catch {
      // URL filtering remains functional when browser storage is unavailable.
    }
    setSearchParams(nextSearchParams, { replace: true });
    window.requestAnimationFrame(() => window.scrollTo({ top: currentWindowTop, behavior: 'auto' }));
  }, [filters, searchParams, setSearchParams, user?.email]);

  const loadConversations = useCallback(async () => {
    const requestId = catalogRequestRef.current + 1;
    catalogRequestRef.current = requestId;
    setLoadingConversations(true);
    setCatalogError(null);
    try {
      const response = await authApi.listExportConversations();
      if (catalogRequestRef.current !== requestId) return;
      setConversations(response.items);
      setCatalogLoaded(true);
    } catch (loadError: unknown) {
      if (catalogRequestRef.current !== requestId) return;
      setCatalogError(loadError);
    } finally {
      if (catalogRequestRef.current === requestId) setLoadingConversations(false);
    }
  }, []);

  useEffect(() => {
    void loadConversations();
    return () => {
      catalogRequestRef.current += 1;
    };
  }, [loadConversations]);

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined;
    const mediaQuery = window.matchMedia(ADMIN_FILTERS_MOBILE_QUERY);
    const syncDisclosure = (event: MediaQueryListEvent) => {
      setFiltersExpanded(!event.matches);
    };
    mediaQuery.addEventListener('change', syncDisclosure);
    return () => mediaQuery.removeEventListener('change', syncDisclosure);
  }, []);

  useEffect(() => {
    if (selectedStorageKeyRef.current !== storedSelectionKey) {
      selectedStorageKeyRef.current = storedSelectionKey;
      setSelectedSessionIds(readStoredSelection(storedSelectionKey));
      return;
    }
    if (!storedSelectionKey) return;
    try {
      window.sessionStorage.setItem(storedSelectionKey, JSON.stringify(selectedSessionIds));
    } catch {
      // Selection remains usable for the current view when browser storage is unavailable.
    }
  }, [selectedSessionIds, storedSelectionKey]);

  useEffect(() => {
    if (!catalogLoaded) return;
    const available = new Set(
      conversations
        .filter((conversation) => conversation.has_conversation)
        .map((conversation) => conversation.session_id),
    );
    setSelectedSessionIds((current) => current.filter((id) => available.has(id)));
  }, [catalogLoaded, conversations, storedSelectionKey]);

  const userOptions = useMemo(() => {
    const users = new Map<string, { email: string; label: string }>();
    conversations.forEach((conversation) => {
      const email = conversation.user_email.trim();
      const key = email.toLowerCase();
      if (!key || users.has(key)) return;
      users.set(key, {
        email: key,
        label: userLabel(conversation),
      });
    });
    return Array.from(users.values()).sort((left, right) => (
      left.label.localeCompare(right.label, language)
      || left.email.localeCompare(right.email)
    ));
  }, [conversations, language]);

  const visibleConversations = useMemo(() => {
    const query = search.trim().toLowerCase();
    return conversations
      .filter((conversation) => {
        if (scope === 'complete' && !conversation.has_rehearsal) return false;
        if (scope === 'incomplete' && conversation.has_rehearsal) return false;
        if (
          selectedUserEmail
          && conversation.user_email.trim().toLowerCase() !== selectedUserEmail
        ) return false;
        const dateKey = conversationDateKey(conversation);
        const hasDateFilter = Boolean(startDate || endDate);
        const dateMatches = hasDateFilter
          ? Boolean(dateKey && (!startDate || dateKey >= startDate) && (!endDate || dateKey <= endDate))
          : true;
        if (!dateMatches) return false;
        if (!query) return true;
        const searchable = [
          conversation.user_email,
          conversation.user_display_name,
          conversation.employee_name,
          conversation.intent_id,
          ...localizedIntentSearchNames(conversation.intent_id),
          conversation.stage,
        ]
          .filter(Boolean)
          .join(' ')
          .toLowerCase();
        return searchable.includes(query);
      })
      .slice()
      .sort((left, right) => {
        const leftTime = conversationActivityDate(left)?.getTime();
        const rightTime = conversationActivityDate(right)?.getTime();
        if (leftTime === undefined && rightTime === undefined) {
          return right.session_id.localeCompare(left.session_id);
        }
        if (leftTime === undefined) return 1;
        if (rightTime === undefined) return -1;
        const dateOrder = sortOrder === 'newest'
          ? rightTime - leftTime
          : leftTime - rightTime;
        return dateOrder || right.session_id.localeCompare(left.session_id);
      });
  }, [
    conversations,
    endDate,
    scope,
    search,
    selectedUserEmail,
    sortOrder,
    startDate,
  ]);

  const visibleSelectableIds = useMemo(
    () => visibleConversations
      .filter((conversation) => conversation.has_conversation)
      .map((conversation) => conversation.session_id),
    [visibleConversations],
  );
  const selectableCount = useMemo(
    () => conversations.filter((conversation) => conversation.has_conversation).length,
    [conversations],
  );
  const completedCount = useMemo(
    () => conversations.filter((conversation) => conversation.has_rehearsal).length,
    [conversations],
  );
  const userCount = useMemo(
    () => new Set(conversations.map((conversation) => conversation.user_email.toLowerCase())).size,
    [conversations],
  );
  const selectedSessionIdSet = useMemo(
    () => new Set(selectedSessionIds),
    [selectedSessionIds],
  );
  const visibleSelectedCount = useMemo(
    () => visibleSelectableIds.filter((id) => selectedSessionIdSet.has(id)).length,
    [selectedSessionIdSet, visibleSelectableIds],
  );
  const hiddenSelectedCount = Math.max(0, selectedSessionIds.length - visibleSelectedCount);
  const allVisibleSelected = visibleSelectableIds.length > 0
    && visibleSelectedCount === visibleSelectableIds.length;
  const filtersActive = hasAdminUsageFilters(filters);
  const detailSearch = adminUsageFilterSearch(searchParams);
  const scrollStorageKey = useMemo(
    () => scrollStorageKeyFor(user?.email, detailSearch),
    [detailSearch, user?.email],
  );
  const restoredScrollKeyRef = useRef('');
  const lastWindowScrollRef = useRef(0);
  const conversationListRef = useRef<HTMLDivElement>(null);
  const catalogErrorMessage = catalogError == null
    ? ''
    : userFacingErrorMessage(
      catalogError,
      undefined,
      translate('无法读取会话目录。', 'Unable to load the conversation catalog.'),
    );
  const detailErrorMessage = detailError == null
    ? ''
    : userFacingErrorMessage(
      detailError,
      undefined,
      translate('无法读取这次使用详情。', 'Unable to load this usage record.'),
    );

  useEffect(() => {
    if (!visibleSelectAllRef.current) return;
    visibleSelectAllRef.current.indeterminate = visibleSelectedCount > 0 && !allVisibleSelected;
  }, [allVisibleSelected, visibleSelectedCount]);

  useEffect(() => {
    if (!catalogLoaded || !scrollStorageKey || restoredScrollKeyRef.current === scrollStorageKey) {
      return undefined;
    }
    restoredScrollKeyRef.current = scrollStorageKey;
    const frame = window.requestAnimationFrame(() => {
      let savedTop = 0;
      try {
        savedTop = Number(window.sessionStorage.getItem(`${scrollStorageKey}:list`)) || 0;
      } catch {
        // A blocked storage API should not prevent the catalog from rendering.
      }
      if (conversationListRef.current) conversationListRef.current.scrollTop = savedTop;
      let savedWindowTop = 0;
      try {
        savedWindowTop = Number(
          window.sessionStorage.getItem(`${scrollStorageKey}:window`),
        ) || 0;
      } catch {
        // A blocked storage API should not prevent the catalog from rendering.
      }
      if (savedWindowTop > 0) {
        lastWindowScrollRef.current = savedWindowTop;
        window.scrollTo({ top: savedWindowTop, behavior: 'auto' });
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [catalogLoaded, scrollStorageKey]);

  useEffect(() => {
    if (!scrollStorageKey) return undefined;
    let frame: number | null = null;
    const saveWindowScroll = () => {
      frame = null;
      try {
        window.sessionStorage.setItem(
          `${scrollStorageKey}:window`,
          String(lastWindowScrollRef.current),
        );
      } catch {
        // Page scrolling remains functional when browser storage is unavailable.
      }
    };
    const scheduleSave = () => {
      lastWindowScrollRef.current = window.scrollY;
      if (frame !== null) return;
      frame = window.requestAnimationFrame(saveWindowScroll);
    };
    lastWindowScrollRef.current = window.scrollY;
    window.addEventListener('scroll', scheduleSave, { passive: true });
    return () => {
      window.removeEventListener('scroll', scheduleSave);
      if (frame !== null) window.cancelAnimationFrame(frame);
    };
  }, [scrollStorageKey]);

  const saveScrollPosition = useCallback(() => {
    if (!scrollStorageKey) return;
    try {
      window.sessionStorage.setItem(
        `${scrollStorageKey}:window`,
        String(lastWindowScrollRef.current || window.scrollY),
      );
      window.sessionStorage.setItem(
        `${scrollStorageKey}:list`,
        String(conversationListRef.current?.scrollTop || 0),
      );
    } catch {
      // Navigation remains functional when browser storage is unavailable.
    }
  }, [scrollStorageKey]);

  const closePendingExport = useCallback(() => {
    if (!activeExportKey) setPendingExport(null);
  }, [activeExportKey]);
  const exportDialogRef = useDialogFocus<HTMLElement, HTMLButtonElement>({
    active: Boolean(pendingExport),
    closeDisabled: Boolean(activeExportKey),
    initialFocusRef: exportCancelButtonRef,
    onClose: closePendingExport,
    returnFocusFallbackRef: exportHeadingRef,
  });

  useEffect(() => () => {
    detailRequestRef.current += 1;
  }, []);

  const closeUsageDetail = useCallback(() => {
    detailRequestRef.current += 1;
    setDetailConversation(null);
    setUsageDetail(null);
    setDetailLoading(false);
    setDetailError(null);
  }, []);

  const loadUsageDetail = useCallback(async (conversation: AdminExportConversation) => {
    const requestId = detailRequestRef.current + 1;
    detailRequestRef.current = requestId;
    setDetailConversation(conversation);
    setUsageDetail(null);
    setDetailError(null);
    setDetailLoading(true);
    try {
      const response = await authApi.getUsageSessionDetail(conversation.session_id);
      if (detailRequestRef.current !== requestId) return;
      setUsageDetail(response);
    } catch (loadError) {
      if (detailRequestRef.current !== requestId) return;
      setDetailError(loadError);
    } finally {
      if (detailRequestRef.current === requestId) setDetailLoading(false);
    }
  }, []);

  const retryUsageDetail = useCallback(() => {
    if (detailConversation) void loadUsageDetail(detailConversation);
  }, [detailConversation, loadUsageDetail]);

  const toggleConversation = (sessionId: string) => {
    setSelectedSessionIds((current) => (
      current.includes(sessionId)
        ? current.filter((value) => value !== sessionId)
        : [...current, sessionId]
    ));
  };

  const selectVisible = () => {
    setSelectedSessionIds((current) => Array.from(new Set([
      ...current,
      ...visibleSelectableIds,
    ])));
  };

  const toggleVisibleSelection = () => {
    if (allVisibleSelected) {
      const visibleIds = new Set(visibleSelectableIds);
      setSelectedSessionIds((current) => current.filter((id) => !visibleIds.has(id)));
      return;
    }
    selectVisible();
  };

  const resetFilters = () => {
    updateFilters(DEFAULT_ADMIN_USAGE_FILTERS);
  };

  const clearHiddenSelection = () => {
    const visibleIds = new Set(visibleSelectableIds);
    setSelectedSessionIds((current) => current.filter((id) => visibleIds.has(id)));
  };

  const requestDownloadSessions = (
    sessionIds: string[],
    exportKey: string,
  ) => {
    setActionError(null);
    setActionMessage(null);
    if (!sessionIds.length) {
      setActionError({ kind: 'validation', code: 'selection_required' });
      return;
    }
    if (startDate && endDate && startDate > endDate) {
      setActionError({ kind: 'validation', code: 'invalid_date_range' });
      return;
    }
    const selectedSessionIdSetForExport = new Set(sessionIds);
    const selectedEmails = new Set(
      conversations
        .filter((conversation) => selectedSessionIdSetForExport.has(conversation.session_id))
        .map((conversation) => conversation.user_email.toLowerCase()),
    );
    const visibleSessionIds = new Set(visibleSelectableIds);
    setPendingExport({
      sessionIds: [...sessionIds],
      exportKey,
      userCount: selectedEmails.size,
      hiddenSelectionCount: sessionIds.filter(
        (sessionId) => !visibleSessionIds.has(sessionId),
      ).length,
    });
  };

  const downloadSessions = async (request: PendingExport) => {
    setPendingExport(null);
    setActiveExportKey(request.exportKey);
    try {
      const { blob, filename } = await authApi.downloadUserContent({
        user_emails: [],
        session_ids: request.sessionIds,
        start_date: null,
        end_date: null,
      });
      saveDownload(blob, filename);
      setActionMessage({ kind: 'export_created', filename });
    } catch (downloadError) {
      setActionError({ kind: 'request', cause: downloadError });
    } finally {
      setActiveExportKey(null);
    }
  };

  return (
    <section className="admin-export-panel">
      <div className="admin-export-heading">
        <div className="admin-section-title">
          <h2 ref={exportHeadingRef} tabIndex={-1}>
            {translate('用户使用记录', 'User usage records')}
          </h2>
          <History size={21} aria-hidden="true" />
        </div>
        <div className="admin-export-heading-actions">
          <span className="admin-export-heading-total">
            {loadingConversations && !catalogLoaded
              ? translate('正在读取', 'Loading')
              : translatedTemplate(
                translate,
                '{count} 次会话',
                '{count} conversations',
                { count: conversations.length },
              )}
          </span>
          <button
            className="icon-button admin-export-refresh"
            type="button"
            onClick={() => void loadConversations()}
            disabled={loadingConversations}
            title={translate('刷新使用记录', 'Refresh usage records')}
            aria-label={translate('刷新使用记录', 'Refresh usage records')}
          >
            <RefreshCw
              className={loadingConversations ? 'admin-loading-icon' : undefined}
              size={17}
              aria-hidden="true"
            />
          </button>
        </div>
      </div>

      <section
        className="admin-export-overview"
        aria-label={translate('导出记录概览', 'Export record overview')}
      >
        <div>
          <span>{translate('涉及用户', 'Users')}</span>
          <strong>{userCount}</strong>
        </div>
        <div>
          <span>{translate('完整预演', 'Completed rehearsals')}</span>
          <strong>{completedCount}</strong>
        </div>
        <div>
          <span>{translate('可导出', 'Exportable')}</span>
          <strong>{selectableCount}</strong>
        </div>
        <div className={selectedSessionIds.length ? 'is-selected' : ''}>
          <span>{translate('已选择', 'Selected')}</span>
          <strong>{selectedSessionIds.length}</strong>
        </div>
      </section>

      <div className="admin-export-users">
        <button
          className="admin-export-filter-toggle"
          type="button"
          aria-expanded={filtersExpanded}
          aria-controls="admin-export-filter-panel"
          onClick={() => setFiltersExpanded((current) => !current)}
        >
          <ListFilter size={17} aria-hidden="true" />
          <span>{translate('筛选与排序', 'Filters and sorting')}</span>
          {filtersActive && <small>{translate('已启用筛选', 'Filters active')}</small>}
          <ChevronDown
            className={filtersExpanded ? 'is-expanded' : undefined}
            size={16}
            aria-hidden="true"
          />
        </button>
        <div
          id="admin-export-filter-panel"
          className="admin-export-filter-bar"
          hidden={!filtersExpanded}
        >
          <label className="admin-export-search">
            <Search size={17} aria-hidden="true" />
            <input
              type="search"
              value={search}
              placeholder={translate(
                '搜索用户、员工或沟通意图',
                'Search users, employees, or conversation intent',
              )}
              aria-label={translate('搜索使用记录', 'Search usage records')}
              onChange={(event) => updateFilters({ query: event.target.value })}
            />
            {search && (
              <button
                className="admin-search-clear"
                type="button"
                onClick={() => updateFilters({ query: '' })}
                title={translate('清除搜索', 'Clear search')}
                aria-label={translate('清除会话搜索', 'Clear conversation search')}
              >
                <X size={15} aria-hidden="true" />
              </button>
            )}
          </label>
          <label className="admin-export-select-field admin-export-user-filter">
            <span>{translate('用户', 'User')}</span>
            <span className="admin-export-select-control">
              <UsersRound size={16} aria-hidden="true" />
              <select
                value={selectedUserEmail}
                onChange={(event) => updateFilters({ userEmail: event.target.value })}
                aria-label={translate('按用户筛选会话', 'Filter conversations by user')}
              >
                <option value="">{translate('全部用户', 'All users')}</option>
                {userOptions.map((option) => (
                  <option key={option.email} value={option.email.toLowerCase()}>
                    {option.label} · {option.email}
                  </option>
                ))}
              </select>
              <ChevronDown size={15} aria-hidden="true" />
            </span>
          </label>
          <label className="admin-export-select-field admin-export-sort-filter">
            <span>{translate('日期排序', 'Date order')}</span>
            <span className="admin-export-select-control">
              <ArrowUpDown size={16} aria-hidden="true" />
              <select
                value={sortOrder}
                onChange={(event) => updateFilters({
                  sortOrder: event.target.value === 'oldest' ? 'oldest' : 'newest',
                })}
                aria-label={translate('会话日期排序', 'Conversation date order')}
              >
                <option value="newest">{translate('最新优先', 'Newest first')}</option>
                <option value="oldest">{translate('最早优先', 'Oldest first')}</option>
              </select>
              <ChevronDown size={15} aria-hidden="true" />
            </span>
          </label>
          <div className="admin-export-date-fields">
            <label>
              <span>{translate('开始日期', 'Start date')}</span>
              <input
                type="date"
                value={startDate}
                max={endDate || undefined}
                onChange={(event) => updateFilters({ startDate: event.target.value })}
              />
            </label>
            <label>
              <span>{translate('结束日期', 'End date')}</span>
              <input
                type="date"
                value={endDate}
                min={startDate || undefined}
                onChange={(event) => updateFilters({ endDate: event.target.value })}
              />
            </label>
          </div>
          <label className="admin-export-select-field admin-export-scope-filter">
            <span>{translate('会话范围', 'Conversation scope')}</span>
            <span className="admin-export-select-control">
              <History size={16} aria-hidden="true" />
              <select
                value={scope}
                onChange={(event) => updateFilters({
                  scope: event.target.value === 'complete'
                    ? 'complete'
                    : event.target.value === 'incomplete'
                      ? 'incomplete'
                      : 'all',
                })}
                aria-label={translate('按完成情况筛选会话', 'Filter conversations by completion')}
              >
                <option value="all">{translate('全部会话', 'All conversations')}</option>
                <option value="complete">{translate('完整预演', 'Completed rehearsals')}</option>
                <option value="incomplete">{translate('未完成预演', 'Incomplete rehearsals')}</option>
              </select>
              <ChevronDown size={15} aria-hidden="true" />
            </span>
          </label>
        </div>

        <div className="admin-export-users-head">
          <div>
            <UsersRound size={18} aria-hidden="true" />
            <strong>{translate('会话记录', 'Conversation records')}</strong>
            <span>
              {translatedTemplate(
                translate,
                '{results} 条结果 · 可见已选 {visible} · 隐藏已选 {hidden}',
                '{results} results · {visible} visible selected · {hidden} hidden selected',
                {
                  results: visibleConversations.length,
                  visible: visibleSelectedCount,
                  hidden: hiddenSelectedCount,
                },
              )}
            </span>
          </div>
          <div className="admin-export-selection-actions">
            <button type="button" onClick={resetFilters} disabled={!filtersActive}>
              {translate('重置筛选', 'Reset filters')}
            </button>
            <button type="button" onClick={selectVisible} disabled={!visibleSelectableIds.length}>
              {translate('选择当前结果', 'Select current results')}
            </button>
            <button
              type="button"
              onClick={clearHiddenSelection}
              disabled={!hiddenSelectedCount}
            >
              {hiddenSelectedCount
                ? translatedTemplate(
                  translate,
                  '清除隐藏选择（{count}）',
                  'Clear hidden selection ({count})',
                  { count: hiddenSelectedCount },
                )
                : translate('清除隐藏选择', 'Clear hidden selection')}
            </button>
            <button
              type="button"
              onClick={() => setSelectedSessionIds([])}
              disabled={!selectedSessionIds.length}
            >
              {translate('清空选择', 'Clear selection')}
            </button>
          </div>
        </div>

        <div
          ref={conversationListRef}
          className="admin-export-user-list"
          role="list"
          aria-busy={loadingConversations}
          aria-label={translate('按时间排列的会话记录', 'Conversation records ordered by time')}
          onScroll={(event) => {
            if (!scrollStorageKey) return;
            try {
              window.sessionStorage.setItem(
                `${scrollStorageKey}:list`,
                String(event.currentTarget.scrollTop),
              );
            } catch {
              // Scrolling remains functional when browser storage is unavailable.
            }
          }}
        >
          <div className="admin-export-list-header">
            <input
              ref={visibleSelectAllRef}
              type="checkbox"
              checked={allVisibleSelected}
              disabled={!visibleSelectableIds.length}
              onChange={toggleVisibleSelection}
              aria-label={allVisibleSelected
                ? translate('取消选择当前结果', 'Deselect current results')
                : translate('选择当前结果', 'Select current results')}
            />
            <span>{translate('会话时间', 'Conversation time')}</span>
            <span>{translate('用户', 'User')}</span>
            <span>{translate('员工与意图', 'Employee and intent')}</span>
            <span>{translate('内容', 'Content')}</span>
            <span>{translate('操作', 'Actions')}</span>
          </div>
          {loadingConversations && !catalogLoaded && (
            <div
              className="admin-export-skeleton"
              role="status"
              aria-label={translate('正在读取会话目录', 'Loading conversation catalog')}
            >
              {[0, 1, 2, 3].map((item) => <span key={item} aria-hidden="true" />)}
            </div>
          )}
          {!loadingConversations && !catalogLoaded && catalogError != null && (
            <div className="admin-export-empty admin-export-load-error" role="alert">
              <AlertTriangle size={22} aria-hidden="true" />
              <p>{catalogErrorMessage}</p>
              <button
                className="btn btn-secondary"
                type="button"
                onClick={() => void loadConversations()}
              >
                <RefreshCw size={16} aria-hidden="true" />
                {translate('重新加载', 'Reload')}
              </button>
            </div>
          )}
          {catalogLoaded && visibleConversations.map((conversation) => {
            const selectable = conversation.has_conversation;
            const displayName = userLabel(conversation);
            return (
              <div
                className={[
                  'admin-export-conversation',
                  conversation.has_rehearsal ? '' : 'is-incomplete',
                  selectable ? '' : 'is-empty',
                ].filter(Boolean).join(' ')}
                key={conversation.session_id}
                role="listitem"
              >
                <input
                  className="admin-export-conversation-select"
                  type="checkbox"
                  aria-label={translatedTemplate(
                    translate,
                    '选择 {name} 的 {date} 会话',
                    "Select {name}'s conversation from {date}",
                    {
                      name: displayName,
                      date: conversationDateLabel(conversation, language, translate),
                    },
                  )}
                  checked={selectedSessionIdSet.has(conversation.session_id)}
                  disabled={!selectable}
                  onChange={() => toggleConversation(conversation.session_id)}
                />
                <span className="admin-export-conversation-time">
                  <strong>
                    <CalendarDays size={14} aria-hidden="true" />
                    {conversationDateLabel(conversation, language, translate)}
                  </strong>
                  <small className={conversation.has_rehearsal ? 'is-complete' : ''}>
                    {rehearsalStatus(conversation, translate)}
                  </small>
                </span>
                <span className="admin-export-user-identity">
                  <button
                    className="admin-export-user-filter-button"
                    type="button"
                    onClick={() => updateFilters({
                      userEmail: conversation.user_email.trim().toLowerCase(),
                    })}
                    aria-label={translatedTemplate(
                      translate,
                      '只查看 {name} 的会话',
                      'Show only conversations for {name}',
                      { name: displayName },
                    )}
                    title={translate('筛选该用户', 'Filter by this user')}
                  >
                    <strong>{displayName}</strong>
                    <small>{conversation.user_email}</small>
                  </button>
                </span>
                <span className="admin-export-conversation-context">
                  <strong>{conversation.employee_name || translate('未记录员工', 'Employee not recorded')}</strong>
                  <small>
                    {conversation.intent_id
                      ? localizedIntentName(conversation.intent_id, language)
                      : translate('未记录沟通意图', 'Conversation intent not recorded')}
                  </small>
                </span>
                <span className="admin-export-conversation-meta">
                  {conversation.turn_count > 0
                    ? translatedTemplate(
                      translate,
                      '{count} 条发言',
                      '{count} turns',
                      { count: conversation.turn_count },
                    )
                    : translate('无对话', 'No conversation')}
                </span>
                <span className="admin-export-row-actions">
                  <Link
                    className="admin-export-row-open admin-export-row-primary"
                    to={`${adminUsageSessionPath(
                      conversation.session_id,
                      defaultAdminUsageSection(conversation),
                    )}${detailSearch}`}
                    aria-label={translatedTemplate(
                      translate,
                      '进入 {name} 的会话页面',
                      'Open the session page for {name}',
                      { name: displayName },
                    )}
                    onClick={saveScrollPosition}
                  >
                    <span>{translate('进入会话', 'Open session')}</span>
                    <ArrowRight size={16} aria-hidden="true" />
                  </Link>
                  <button
                    className="admin-export-row-view"
                    type="button"
                    aria-label={translatedTemplate(
                      translate,
                      '查看 {name} 的这次使用详情',
                      'View this usage record for {name}',
                      { name: displayName },
                    )}
                    title={translate('查看这次使用详情', 'View this usage record')}
                    onClick={() => void loadUsageDetail(conversation)}
                  >
                    <Eye size={17} aria-hidden="true" />
                  </button>
                  <button
                    className="admin-export-row-download"
                    type="button"
                    aria-label={translatedTemplate(
                      translate,
                      '下载 {name} 的这次会话',
                      'Download this conversation for {name}',
                      { name: displayName },
                    )}
                    title={selectable
                      ? translate('下载这次会话', 'Download this conversation')
                      : translate(
                        '该会话尚无可下载的对话',
                        'This session does not have a conversation available for download',
                      )}
                    disabled={busy || Boolean(activeExportKey) || !selectable}
                    onClick={() => requestDownloadSessions(
                      [conversation.session_id],
                      conversation.session_id,
                    )}
                  >
                    {activeExportKey === conversation.session_id
                      ? <LoaderCircle className="admin-loading-icon" size={17} aria-hidden="true" />
                      : <Download size={17} aria-hidden="true" />}
                  </button>
                </span>
              </div>
            );
          })}
          {catalogLoaded && !visibleConversations.length && (
            <div className="admin-export-empty">
              {scope === 'complete'
                ? translate(
                  '没有匹配的完整预演记录，可调整会话范围查看其他会话。',
                  'No completed rehearsals match. Change the conversation scope to view other sessions.',
                )
                : scope === 'incomplete'
                  ? translate('没有匹配的未完成预演记录。', 'No incomplete rehearsals match.')
                  : translate('没有匹配的会话记录。', 'No conversation records match.')}
            </div>
          )}
        </div>
      </div>

      {((catalogLoaded && catalogErrorMessage) || error || message) && (
        <div
          className={((catalogLoaded && catalogErrorMessage) || error) ? 'auth-error' : 'auth-success'}
          role={((catalogLoaded && catalogErrorMessage) || error) ? 'alert' : 'status'}
          aria-live="polite"
        >
          {(catalogLoaded && catalogErrorMessage) || error || message}
        </div>
      )}
      <div className={`admin-export-footer${selectedSessionIds.length ? ' has-selection' : ''}`}>
        <span>{translate(
          '可单独下载任一会话，也可勾选多次会话统一打包为 ZIP。',
          'Download any conversation individually, or select multiple conversations to package as a ZIP file.',
        )}</span>
        <button
          className="btn btn-secondary"
          type="button"
          disabled={
            busy
            || loadingConversations
            || !catalogLoaded
            || Boolean(activeExportKey)
            || !selectedSessionIds.length
          }
          onClick={() => requestDownloadSessions(selectedSessionIds, 'batch')}
        >
          {activeExportKey === 'batch'
            ? <LoaderCircle className="admin-loading-icon" size={18} aria-hidden="true" />
            : <Download size={18} aria-hidden="true" />}
          {activeExportKey === 'batch'
            ? translate('正在生成', 'Generating')
            : translatedTemplate(
              translate,
              '导出 {count} 次会话',
              'Export {count} conversations',
              { count: selectedSessionIds.length },
            )}
        </button>
      </div>

      {pendingExport && (
        <div
          className="modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closePendingExport();
          }}
        >
          <section
            ref={exportDialogRef}
            className="admin-confirm-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="admin-export-confirm-title"
            aria-describedby="admin-export-confirm-description"
            tabIndex={-1}
          >
            <div className="admin-confirm-dialog-icon is-warning" aria-hidden="true">
              <AlertTriangle size={21} />
            </div>
            <div className="admin-confirm-dialog-copy">
              <h2 id="admin-export-confirm-title">
                {translate('确认导出沟通记录', 'Confirm conversation record export')}
              </h2>
              <p id="admin-export-confirm-description">
                {translate(
                  '文件包含员工资料、指导、预演对话和复盘报告，请仅在受控环境中保存和使用。',
                  'The file contains employee profiles, guidance, rehearsal conversations, and review reports. Store and use it only in a controlled environment.',
                )}
              </p>
              <div
                className="admin-confirm-summary"
                aria-label={translate('本次导出范围', 'Export scope')}
              >
                <span>
                  <strong>{pendingExport.userCount}</strong>{' '}
                  {translate('个用户', 'users')}
                </span>
                <span>
                  <strong>{pendingExport.sessionIds.length}</strong>{' '}
                  {translate('次会话', 'conversations')}
                </span>
                {pendingExport.hiddenSelectionCount > 0 && (
                  <span>
                    <strong>{pendingExport.hiddenSelectionCount}</strong>{' '}
                    {translate('次当前不可见', 'currently hidden')}
                  </span>
                )}
              </div>
            </div>
            <div className="dialog-actions">
              <button
                ref={exportCancelButtonRef}
                className="btn btn-secondary"
                type="button"
                onClick={closePendingExport}
              >
                {translate('取消', 'Cancel')}
              </button>
              <button
                className="btn btn-primary"
                type="button"
                onClick={() => void downloadSessions(pendingExport)}
              >
                <Download size={17} aria-hidden="true" />
                {translate('确认导出', 'Confirm export')}
              </button>
            </div>
          </section>
        </div>
      )}
      {detailConversation && (
        <AdminUsageSessionDialog
          conversation={detailConversation}
          detail={usageDetail}
          loading={detailLoading}
          error={detailErrorMessage}
          onClose={closeUsageDetail}
          onRetry={retryUsageDetail}
          onOpenPage={saveScrollPosition}
        />
      )}
    </section>
  );
}

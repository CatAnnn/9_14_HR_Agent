import { Search } from 'lucide-react';
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';
import { useLanguage } from '../i18n/LanguageContext';
import '../styles/landing-site-search.css';

const LandingSiteSearchPanel = lazy(() => import('./LandingSiteSearchPanel'));

interface LandingSiteSearchProps {
  onOpen?: () => void;
  onOpenChange?: (open: boolean) => void;
}

interface SearchLoadingHeaderProps {
  onClose: () => void;
}

function SearchLoadingHeader({ onClose }: SearchLoadingHeaderProps) {
  const { translate } = useLanguage();

  return (
    <section id="landing-site-search" className="landing-site-search-surface is-loading" role="status">
      <div
        className="hamburger-curtain"
        aria-hidden="true"
        onClick={onClose}
      />
      <div className="landing-site-search-bar">
        <span className="landing-site-search-loader" aria-hidden="true" />
        <span>{translate('正在加载搜索')}</span>
        <button type="button" onClick={onClose} aria-label={translate('关闭搜索')}>
          <span aria-hidden="true">×</span>
        </button>
      </div>
    </section>
  );
}

export function LandingSiteSearch({ onOpen, onOpenChange }: LandingSiteSearchProps) {
  const { translate } = useLanguage();
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const portalTarget = triggerRef.current?.closest<HTMLElement>('.home-header') ?? null;

  const closeSearch = useCallback(() => {
    setOpen(false);
    onOpenChange?.(false);
    window.requestAnimationFrame(() => triggerRef.current?.focus());
  }, [onOpenChange]);

  const openSearch = useCallback(() => {
    onOpen?.();
    setOpen(true);
    onOpenChange?.(true);
  }, [onOpen, onOpenChange]);

  useEffect(() => {
    if (!open) return undefined;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeSearch();
    };

    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [closeSearch, open]);

  return (
    <>
      <button
        ref={triggerRef}
        className="landing-site-search-trigger"
        type="button"
        aria-label={translate('搜索网站')}
        title={translate('搜索')}
        aria-expanded={open}
        aria-controls="landing-site-search"
        onClick={openSearch}
      >
        <Search aria-hidden="true" />
      </button>

      {open && portalTarget && createPortal(
        <Suspense fallback={<SearchLoadingHeader onClose={closeSearch} />}>
          <LandingSiteSearchPanel onClose={closeSearch} />
        </Suspense>,
        portalTarget,
      )}
    </>
  );
}

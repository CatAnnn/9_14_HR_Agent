import { Check, ChevronDown, Languages } from 'lucide-react';
import {
  useEffect,
  useId,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import {
  languageName,
  normalizeAppLanguage,
  SUPPORTED_LANGUAGES,
  useLanguage,
  type AppLanguage,
} from '../i18n/LanguageContext';
import '../styles/language-selector.css';

export interface LanguageSelectorProps {
  className?: string;
  compact?: boolean;
  onOpen?: () => void;
  variant?: 'native' | 'popover';
}

const LANGUAGE_FLAG_PATHS: Readonly<Record<AppLanguage, string>> = {
  'zh-CN': '/assets/brand/flag-zh.svg',
  en: '/assets/brand/flag-en.svg',
  de: '/assets/brand/flag-de.svg',
  ja: '/assets/brand/flag-ja.svg',
};

function selectorClassName(
  className: string,
  compact: boolean,
  variant?: 'native' | 'popover',
  open?: boolean,
): string {
  return [
    'language-selector',
    compact ? 'is-compact' : '',
    variant === 'popover' ? 'is-popover' : '',
    open ? 'is-open' : '',
    className,
  ].filter(Boolean).join(' ');
}

function NativeLanguageSelector({
  className = '',
  compact = false,
}: LanguageSelectorProps) {
  const { language, setLanguage, translate } = useLanguage();

  const selectLanguage = (event: ChangeEvent<HTMLSelectElement>) => {
    const nextLanguage = normalizeAppLanguage(event.target.value);
    if (nextLanguage) setLanguage(nextLanguage);
  };

  return (
    <label
      className={selectorClassName(className, compact)}
      data-language-selector=""
    >
      <Languages aria-hidden="true" />
      <select
        value={language}
        onChange={selectLanguage}
        aria-label={translate('语言', 'Language')}
      >
        {SUPPORTED_LANGUAGES.map((locale) => (
          <option key={locale} value={locale} lang={locale}>{languageName(locale)}</option>
        ))}
      </select>
    </label>
  );
}

function PopoverLanguageSelector({
  className = '',
  compact = false,
  onOpen,
}: LanguageSelectorProps) {
  const { language, setLanguage, translate } = useLanguage();
  const [menuOpen, setMenuOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const menuId = `${useId()}-language-menu`;
  const selectedIndex = Math.max(0, SUPPORTED_LANGUAGES.indexOf(language));

  const focusOption = (index: number) => {
    const optionCount = SUPPORTED_LANGUAGES.length;
    const normalizedIndex = ((index % optionCount) + optionCount) % optionCount;
    optionRefs.current[normalizedIndex]?.focus();
  };

  const openMenu = () => {
    onOpen?.();
    setMenuOpen(true);
  };

  const closeMenu = (restoreTriggerFocus = false) => {
    setMenuOpen(false);
    if (restoreTriggerFocus) {
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    }
  };

  useEffect(() => {
    if (!menuOpen) return undefined;

    const frame = window.requestAnimationFrame(() => focusOption(selectedIndex));
    const closeOnPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) closeMenu();
    };
    const closeOnFocusIn = (event: FocusEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) closeMenu();
    };

    window.addEventListener('pointerdown', closeOnPointerDown);
    window.addEventListener('focusin', closeOnFocusIn);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener('pointerdown', closeOnPointerDown);
      window.removeEventListener('focusin', closeOnFocusIn);
    };
  }, [menuOpen, selectedIndex]);

  const selectLanguage = (locale: AppLanguage) => {
    const nextLanguage = normalizeAppLanguage(locale);
    if (nextLanguage) setLanguage(nextLanguage);
    closeMenu(true);
  };

  const handleTriggerKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    event.preventDefault();
    openMenu();
  };

  const handleMenuKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const activeIndex = optionRefs.current.findIndex((option) => option === document.activeElement);

    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeMenu(true);
      return;
    }

    if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      focusOption(event.key === 'Home' ? 0 : SUPPORTED_LANGUAGES.length - 1);
      return;
    }

    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const fallbackIndex = activeIndex < 0 ? selectedIndex : activeIndex;
      focusOption(fallbackIndex + (event.key === 'ArrowDown' ? 1 : -1));
    }
  };

  return (
    <div
      ref={rootRef}
      className={selectorClassName(className, compact, 'popover', menuOpen)}
      data-language-selector=""
    >
      <button
        ref={triggerRef}
        className="language-selector-trigger"
        type="button"
        aria-label={`${translate('语言', 'Language')}: ${languageName(language)}`}
        aria-haspopup="listbox"
        aria-expanded={menuOpen}
        aria-controls={menuId}
        onClick={() => (menuOpen ? closeMenu() : openMenu())}
        onKeyDown={handleTriggerKeyDown}
      >
        <Languages className="language-selector-leading-icon" aria-hidden="true" />
        <span className="language-selector-label" lang={language}>{languageName(language)}</span>
        <ChevronDown className="language-selector-chevron" aria-hidden="true" />
      </button>

      {menuOpen && (
        <div
          id={menuId}
          className="language-selector-menu"
          role="listbox"
          aria-label={translate('语言', 'Language')}
          onKeyDown={handleMenuKeyDown}
        >
          {SUPPORTED_LANGUAGES.map((locale, index) => {
            const selected = locale === language;
            return (
              <button
                key={locale}
                ref={(element) => { optionRefs.current[index] = element; }}
                className="language-selector-option"
                type="button"
                role="option"
                aria-selected={selected}
                lang={locale}
                tabIndex={-1}
                onClick={() => selectLanguage(locale)}
              >
                <img
                  className="language-selector-option-flag"
                  src={LANGUAGE_FLAG_PATHS[locale]}
                  alt=""
                  width="48"
                  height="38"
                  decoding="async"
                  draggable={false}
                />
                <span className="language-selector-option-label">{languageName(locale)}</span>
                <Check className="language-selector-check" aria-hidden="true" />
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function LanguageSelector(props: LanguageSelectorProps) {
  if (props.variant === 'popover') return <PopoverLanguageSelector {...props} />;
  return <NativeLanguageSelector {...props} />;
}

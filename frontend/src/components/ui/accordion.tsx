import {
  createContext,
  forwardRef,
  useCallback,
  useContext,
  useId,
  useMemo,
  useState,
  type HTMLAttributes,
  type Key,
  type MouseEvent,
} from 'react';
import { ChevronDown } from 'lucide-react';

import './accordion.css';
import { createExpandedKeySet, toggleExpandedKey } from './accordionState';

interface AccordionContextValue {
  expandedKeys: ReadonlySet<Key>;
  toggleKey: (key: Key) => void;
}

interface AccordionItemContextValue {
  contentId: string;
  expanded: boolean;
  toggle: () => void;
  triggerId: string;
}

const AccordionContext = createContext<AccordionContextValue | null>(null);
const AccordionItemContext = createContext<AccordionItemContextValue | null>(null);

function classNames(...values: Array<string | undefined>) {
  return values.filter(Boolean).join(' ');
}

function useAccordionContext(componentName: string) {
  const context = useContext(AccordionContext);
  if (!context) {
    throw new Error(`${componentName} must be used inside Accordion.`);
  }
  return context;
}

function useAccordionItemContext(componentName: string) {
  const context = useContext(AccordionItemContext);
  if (!context) {
    throw new Error(`${componentName} must be used inside AccordionItem.`);
  }
  return context;
}

export interface AccordionProps extends HTMLAttributes<HTMLDivElement> {
  defaultExpandedKeys?: Iterable<Key>;
}

export const Accordion = forwardRef<HTMLDivElement, AccordionProps>(function Accordion(
  { className, defaultExpandedKeys, ...props },
  ref,
) {
  const [expandedKeys, setExpandedKeys] = useState<Set<Key>>(
    () => createExpandedKeySet(defaultExpandedKeys),
  );

  const toggleKey = useCallback((key: Key) => {
    setExpandedKeys((currentKeys) => toggleExpandedKey(currentKeys, key));
  }, []);

  const contextValue = useMemo<AccordionContextValue>(
    () => ({ expandedKeys, toggleKey }),
    [expandedKeys, toggleKey],
  );

  return (
    <AccordionContext.Provider value={contextValue}>
      <div
        {...props}
        ref={ref}
        className={classNames('ui-accordion', className)}
        data-slot="accordion"
      />
    </AccordionContext.Provider>
  );
});

export interface AccordionItemProps extends Omit<HTMLAttributes<HTMLDivElement>, 'id'> {
  id: Key;
}

export const AccordionItem = forwardRef<HTMLDivElement, AccordionItemProps>(function AccordionItem(
  { className, id, ...props },
  ref,
) {
  const { expandedKeys, toggleKey } = useAccordionContext('AccordionItem');
  const generatedId = useId().replace(/:/g, '');
  const expanded = expandedKeys.has(id);
  const triggerId = `accordion-trigger-${generatedId}`;
  const contentId = `accordion-content-${generatedId}`;
  const toggle = useCallback(() => toggleKey(id), [id, toggleKey]);
  const contextValue = useMemo<AccordionItemContextValue>(
    () => ({ contentId, expanded, toggle, triggerId }),
    [contentId, expanded, toggle, triggerId],
  );

  return (
    <AccordionItemContext.Provider value={contextValue}>
      <div
        {...props}
        ref={ref}
        className={classNames('ui-accordion-item', className)}
        data-accordion-id={String(id)}
        data-slot="accordion-item"
        data-state={expanded ? 'open' : 'closed'}
      />
    </AccordionItemContext.Provider>
  );
});

const NESTED_INTERACTIVE_SELECTOR = [
  'button',
  'a',
  'input',
  'select',
  'textarea',
  '[role="button"]',
  '[role="link"]',
].join(',');

function cameFromNestedInteractiveElement(
  event: MouseEvent<HTMLDivElement>,
) {
  if (!(event.target instanceof Element)) return false;
  const interactiveElement = event.target.closest(NESTED_INTERACTIVE_SELECTOR);
  return Boolean(
    interactiveElement
    && interactiveElement !== event.currentTarget
    && event.currentTarget.contains(interactiveElement),
  );
}

export type AccordionTriggerProps = HTMLAttributes<HTMLDivElement>;

export const AccordionTrigger = forwardRef<HTMLDivElement, AccordionTriggerProps>(
  function AccordionTrigger(
    {
      children,
      className,
      onClick,
      ...props
    },
    ref,
  ) {
    const {
      contentId,
      expanded,
      toggle,
      triggerId,
    } = useAccordionItemContext('AccordionTrigger');
    const labelId = `${triggerId}-label`;

    const handleClick = (event: MouseEvent<HTMLDivElement>) => {
      if (cameFromNestedInteractiveElement(event)) return;
      onClick?.(event);
      if (!event.defaultPrevented) toggle();
    };

    const handleControlClick = (event: MouseEvent<HTMLButtonElement>) => {
      event.stopPropagation();
      toggle();
    };

    return (
      <div
        {...props}
        ref={ref}
        className={classNames('ui-accordion-trigger', className)}
        data-slot="accordion-trigger"
        data-state={expanded ? 'open' : 'closed'}
        onClick={handleClick}
      >
        <span className="ui-accordion-trigger-label" id={labelId}>{children}</span>
        <button
          className="ui-accordion-trigger-control"
          id={triggerId}
          type="button"
          aria-controls={contentId}
          aria-expanded={expanded}
          aria-labelledby={labelId}
          data-slot="accordion-trigger-control"
          data-state={expanded ? 'open' : 'closed'}
          onClick={handleControlClick}
        >
          <ChevronDown
            className="ui-accordion-trigger-icon"
            size={16}
            strokeWidth={2}
            aria-hidden="true"
          />
        </button>
      </div>
    );
  },
);

export type AccordionContentProps = HTMLAttributes<HTMLDivElement>;

export const AccordionContent = forwardRef<HTMLDivElement, AccordionContentProps>(
  function AccordionContent({ children, className, ...props }, ref) {
    const { contentId, expanded, triggerId } = useAccordionItemContext('AccordionContent');

    return (
      <div
        {...props}
        ref={ref}
        id={contentId}
        className={classNames('ui-accordion-content', className)}
        role="region"
        aria-hidden={!expanded}
        aria-labelledby={triggerId}
        data-slot="accordion-content"
        data-state={expanded ? 'open' : 'closed'}
        inert={expanded ? undefined : true}
      >
        <div className="ui-accordion-content-inner">{children}</div>
      </div>
    );
  },
);

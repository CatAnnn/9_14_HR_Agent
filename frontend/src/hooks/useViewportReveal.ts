import { useLayoutEffect, type RefObject } from 'react';

const DEFAULT_REVEAL_SELECTOR = '[data-viewport-reveal], [data-home-reveal]';
const DESKTOP_MOTION_QUERY = '(min-width: 1024px) and (prefers-reduced-motion: no-preference)';
const RESETTING_CLASS = 'is-viewport-reveal-resetting';
const REVEAL_TRANSITION_PROPERTIES = new Set(['opacity', 'translate', 'scale']);
const MAX_REVEAL_DELAY_MS = 220;
const REVEAL_SETTLE_MS = 1100 + MAX_REVEAL_DELAY_MS;

interface ViewportRevealOptions {
  resetKey?: string;
  selector?: string;
}

function revealDelay(element: HTMLElement): number {
  const parsed = Number(element.dataset.viewportRevealDelay);
  if (!Number.isFinite(parsed)) return 0;
  return Math.min(MAX_REVEAL_DELAY_MS, Math.max(0, Math.round(parsed)));
}

/**
 * Adds a one-time, desktop-only entrance motion to explicitly selected content.
 * Elements already above the lower viewport trigger line remain immediately visible,
 * which keeps route restoration and hash navigation free from flashes.
 */
export function useViewportReveal<T extends HTMLElement>(
  rootRef: RefObject<T | null>,
  {
    resetKey = '',
    selector = DEFAULT_REVEAL_SELECTOR,
  }: ViewportRevealOptions = {},
): void {
  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!root || typeof window.matchMedia !== 'function') return undefined;

    const motionQuery = window.matchMedia(DESKTOP_MOTION_QUERY);
    const settleTimers = new Map<HTMLElement, number>();
    let observer: IntersectionObserver | null = null;
    let prepareFrame = 0;
    let armFrame = 0;

    const targets = () => Array.from(root.querySelectorAll<HTMLElement>(selector))
      .filter((element) => (
        element.dataset.viewportReveal !== 'off'
        && !element.closest('[data-viewport-reveal-scope="off"]')
      ));

    const clearElementState = (element: HTMLElement) => {
      element.classList.remove(
        'is-viewport-reveal-pending',
        'is-viewport-reveal-armed',
        'is-viewport-reveal-visible',
      );
      element.style.removeProperty('--viewport-reveal-delay');
    };

    const cancelRevealTransitions = (element: HTMLElement) => {
      element.getAnimations().forEach((animation) => {
        const transitionProperty = (animation as Animation & {
          transitionProperty?: string;
        }).transitionProperty;
        if (transitionProperty && REVEAL_TRANSITION_PROPERTIES.has(transitionProperty)) {
          animation.cancel();
        }
      });
    };

    const cancelArmFrames = () => {
      if (prepareFrame) window.cancelAnimationFrame(prepareFrame);
      if (armFrame) window.cancelAnimationFrame(armFrame);
      prepareFrame = 0;
      armFrame = 0;
    };

    const clearMotionState = () => {
      cancelArmFrames();
      observer?.disconnect();
      observer = null;
      settleTimers.forEach((timer) => window.clearTimeout(timer));
      settleTimers.clear();
      const motionTargets = targets();
      motionTargets.forEach((element) => {
        // Cancel any in-flight reveal transition before removing its state. Without
        // this reset, changing reduced-motion while an element is pending can leave
        // Chromium holding the old opacity/translate at a zero-length transition.
        element.classList.add(RESETTING_CLASS);
        cancelRevealTransitions(element);
        clearElementState(element);
        cancelRevealTransitions(element);
        element.classList.remove(RESETTING_CLASS);
      });
    };

    const reveal = (element: HTMLElement) => {
      observer?.unobserve(element);
      element.classList.add('is-viewport-reveal-visible');
      const timer = window.setTimeout(() => {
        clearElementState(element);
        settleTimers.delete(element);
      }, REVEAL_SETTLE_MS);
      settleTimers.set(element, timer);
    };

    const setup = () => {
      clearMotionState();
      if (!motionQuery.matches || typeof IntersectionObserver === 'undefined') return;

      const activationLine = window.innerHeight * 0.88;
      observer = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          reveal(entry.target as HTMLElement);
        });
      }, {
        rootMargin: '0px 0px -12% 0px',
        threshold: 0.12,
      });

      // Read layout for every target before mutating any reveal styles or classes.
      const pendingElements = targets()
        .map((element) => ({
          element,
          top: element.getClientRects().length > 0
            ? element.getBoundingClientRect().top
            : null,
          delay: revealDelay(element),
        }))
        // Anything already reached by the restored/current viewport must never flash hidden.
        .filter(({ top }) => top !== null && top > activationLine);

      // Apply the hidden starting pose in one write phase after all rect reads finish.
      pendingElements.forEach(({ element, delay }) => {
        if (delay > 0) {
          element.style.setProperty('--viewport-reveal-delay', `${delay}ms`);
        }
        element.classList.add('is-viewport-reveal-pending');
      });

      if (pendingElements.length === 0) return;

      // Give the pending pose a paint before transitions are armed. Splitting this
      // across frames avoids a synchronous layout flush and the reverse animation
      // that occurs when pending and armed are committed together.
      prepareFrame = window.requestAnimationFrame(() => {
        prepareFrame = 0;
        armFrame = window.requestAnimationFrame(() => {
          armFrame = 0;
          pendingElements.forEach(({ element }) => {
            if (!root.contains(element)) return;
            element.classList.add('is-viewport-reveal-armed');
            observer?.observe(element);
          });
        });
      });
    };

    setup();
    motionQuery.addEventListener('change', setup);

    return () => {
      motionQuery.removeEventListener('change', setup);
      clearMotionState();
    };
  }, [resetKey, rootRef, selector]);
}

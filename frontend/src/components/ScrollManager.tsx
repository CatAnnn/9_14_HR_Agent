import { useEffect, useLayoutEffect, useRef } from 'react';
import { useLocation, useNavigationType } from 'react-router-dom';

const scrollPositions = new Map<string, { left: number; top: number }>();

function hashTarget(hash: string): HTMLElement | null {
  if (!hash) return null;
  try {
    return document.getElementById(decodeURIComponent(hash.slice(1)));
  } catch {
    return document.getElementById(hash.slice(1));
  }
}

export function ScrollManager() {
  const location = useLocation();
  const navigationType = useNavigationType();
  const currentKeyRef = useRef(location.key);
  currentKeyRef.current = location.key;

  useEffect(() => {
    const previousMode = window.history.scrollRestoration;
    window.history.scrollRestoration = 'manual';
    return () => {
      window.history.scrollRestoration = previousMode;
    };
  }, []);

  useEffect(() => {
    let saveFrame: number | null = null;

    const writePosition = () => {
      const key = currentKeyRef.current;
      const left = window.scrollX;
      const top = window.scrollY;
      const previous = scrollPositions.get(key);
      if (previous?.left === left && previous.top === top) return;

      scrollPositions.set(key, { left, top });
      if (scrollPositions.size > 80) {
        const oldestKey = scrollPositions.keys().next().value;
        if (oldestKey) scrollPositions.delete(oldestKey);
      }
    };

    const cancelScheduledSave = () => {
      if (saveFrame === null) return;
      window.cancelAnimationFrame(saveFrame);
      saveFrame = null;
    };

    const flushPosition = () => {
      cancelScheduledSave();
      writePosition();
    };

    const schedulePositionSave = () => {
      if (saveFrame !== null) return;
      saveFrame = window.requestAnimationFrame(() => {
        saveFrame = null;
        writePosition();
      });
    };

    const flushBeforeSameOriginNavigation = (event: MouseEvent) => {
      if (
        event.defaultPrevented
        || event.button !== 0
        || event.metaKey
        || event.ctrlKey
        || event.shiftKey
        || event.altKey
      ) return;

      const target = event.target;
      if (!(target instanceof Element)) return;
      const anchor = target.closest<HTMLAnchorElement>('a[href]');
      if (
        !anchor
        || anchor.hasAttribute('download')
        || (anchor.target && anchor.target.toLowerCase() !== '_self')
      ) return;

      const destination = new URL(anchor.href, window.location.href);
      if (destination.origin !== window.location.origin) return;
      flushPosition();
    };

    writePosition();
    window.addEventListener('scroll', schedulePositionSave, { passive: true });
    window.addEventListener('popstate', flushPosition);
    window.addEventListener('pagehide', flushPosition);
    document.addEventListener('click', flushBeforeSameOriginNavigation, true);
    return () => {
      cancelScheduledSave();
      window.removeEventListener('scroll', schedulePositionSave);
      window.removeEventListener('popstate', flushPosition);
      window.removeEventListener('pagehide', flushPosition);
      document.removeEventListener('click', flushBeforeSameOriginNavigation, true);
    };
  }, []);

  useLayoutEffect(() => {
    const target = hashTarget(location.hash);
    if (target) {
      target.scrollIntoView({ behavior: 'auto', block: 'start' });
      return;
    }

    const savedPosition = navigationType === 'POP'
      ? scrollPositions.get(location.key)
      : undefined;
    window.scrollTo({
      behavior: 'auto',
      left: savedPosition?.left ?? 0,
      top: savedPosition?.top ?? 0,
    });
  }, [location.hash, location.key, location.pathname, navigationType]);

  return null;
}

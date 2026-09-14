import { useEffect, type RefObject } from 'react';
import { backgroundParallaxOffset } from '../utils/backgroundParallax';

/** Scroll-driven decoration only; content and layout never depend on motion. */
export function useBackgroundParallax(
  sectionRef: RefObject<HTMLElement | null>,
  mediaRef: RefObject<HTMLElement | null>,
): void {
  useEffect(() => {
    const section = sectionRef.current;
    const media = mediaRef.current;
    if (!section || !media || typeof window.matchMedia !== 'function') return;

    const motion = window.matchMedia('(min-width: 1024px) and (prefers-reduced-motion: no-preference)');
    let inView = typeof IntersectionObserver === 'undefined';
    let frame = 0;
    const canAnimate = () => inView && motion.matches && !document.hidden;

    const update = () => {
      frame = 0;
      if (!canAnimate()) return;
      const { top, height } = section.getBoundingClientRect();
      const offset = backgroundParallaxOffset(top, height, window.innerHeight);
      media.style.setProperty('--background-parallax-offset', `${offset.toFixed(2)}px`);
    };
    const schedule = () => {
      if (canAnimate() && !frame) frame = window.requestAnimationFrame(update);
    };
    const sync = () => {
      if (canAnimate()) {
        media.dataset.parallaxActive = 'true';
        schedule();
      } else {
        window.cancelAnimationFrame(frame);
        frame = 0;
        delete media.dataset.parallaxActive;
        media.style.removeProperty('--background-parallax-offset');
      }
    };

    const observer = typeof IntersectionObserver === 'undefined' ? null
      : new IntersectionObserver(([entry]) => {
        inView = Boolean(entry?.isIntersecting);
        sync();
      });
    observer?.observe(section);
    // Also update when a locale change or text reflow changes the section height.
    const resizeObserver = typeof ResizeObserver === 'undefined' ? null
      : new ResizeObserver(schedule);
    resizeObserver?.observe(section);
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    document.addEventListener('visibilitychange', sync);
    motion.addEventListener('change', sync);
    sync();

    return () => {
      observer?.disconnect();
      resizeObserver?.disconnect();
      window.removeEventListener('scroll', schedule);
      window.removeEventListener('resize', schedule);
      document.removeEventListener('visibilitychange', sync);
      motion.removeEventListener('change', sync);
      window.cancelAnimationFrame(frame);
      delete media.dataset.parallaxActive;
      media.style.removeProperty('--background-parallax-offset');
    };
  }, [sectionRef, mediaRef]);
}

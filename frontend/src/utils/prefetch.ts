interface NetworkInformation {
  saveData?: boolean;
  effectiveType?: string;
}

interface PrefetchNavigator extends Navigator {
  connection?: NetworkInformation;
}

interface IdlePrefetchOptions {
  delayMs?: number;
  idleTimeoutMs?: number;
}

export function shouldPrefetch(): boolean {
  if (document.visibilityState === 'hidden') return false;
  const connection = (navigator as PrefetchNavigator).connection;
  const effectiveType = connection?.effectiveType;
  return !connection?.saveData && (!effectiveType || effectiveType === '4g');
}

export function scheduleIdlePrefetch(
  load: () => Promise<unknown>,
  { delayMs = 0, idleTimeoutMs = 1600 }: IdlePrefetchOptions = {},
): () => void {
  if (!shouldPrefetch()) return () => undefined;

  let cancelled = false;
  let idleId: number | undefined;
  let timeoutId: ReturnType<typeof globalThis.setTimeout> | undefined;
  const run = () => {
    if (!cancelled && shouldPrefetch()) void load().catch(() => undefined);
  };

  const requestIdle = Reflect.get(window, 'requestIdleCallback') as typeof window.requestIdleCallback | undefined;
  const cancelIdle = Reflect.get(window, 'cancelIdleCallback') as typeof window.cancelIdleCallback | undefined;
  const schedule = () => {
    if (cancelled) return;
    if (typeof requestIdle === 'function') {
      idleId = requestIdle.call(window, run, { timeout: idleTimeoutMs });
      return;
    }

    timeoutId = globalThis.setTimeout(run, 400);
  };

  if (delayMs > 0) timeoutId = globalThis.setTimeout(schedule, delayMs);
  else schedule();

  return () => {
    cancelled = true;
    if (idleId !== undefined) cancelIdle?.call(window, idleId);
    if (timeoutId !== undefined) globalThis.clearTimeout(timeoutId);
  };
}

import { lazy, type ComponentType, type LazyExoticComponent } from 'react';

interface RouteModule<T extends ComponentType> {
  default: T;
}

export type PreloadableRoute<T extends ComponentType = ComponentType> =
  LazyExoticComponent<T> & {
    preload: () => Promise<void>;
  };

const RETRYABLE_IMPORT_ERROR = /(?:chunkloaderror|failed to fetch|dynamically imported module|importing a module script failed|loading css chunk)/i;
const IMPORT_RETRY_DELAY_MS = 120;

function isRetryableImportError(error: unknown): boolean {
  const name = error instanceof Error ? error.name : '';
  const message = error instanceof Error ? error.message : String(error || '');
  return RETRYABLE_IMPORT_ERROR.test(name + ' ' + message);
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => {
    globalThis.setTimeout(resolve, milliseconds);
  });
}

export function lazyRoute<T extends ComponentType>(
  loader: () => Promise<RouteModule<T>>,
): PreloadableRoute<T> {
  let pending: Promise<RouteModule<T>> | undefined;

  const load = (): Promise<RouteModule<T>> => {
    if (!pending) {
      pending = loader()
        .catch(async (error: unknown) => {
          if (!isRetryableImportError(error)) throw error;
          await delay(IMPORT_RETRY_DELAY_MS);
          return loader();
        })
        .catch((error: unknown) => {
          pending = undefined;
          throw error;
        });
    }
    return pending;
  };

  return Object.assign(lazy(load), {
    preload: async () => {
      await load();
    },
  });
}

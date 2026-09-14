export interface SerialMessageQueueSnapshot {
  active: boolean;
  paused: boolean;
  size: number;
  waiting: number;
}

export interface SerialMessageQueue<T> {
  enqueue: (item: T) => void;
  retry: () => boolean;
  clearPending: () => void;
  getSnapshot: () => SerialMessageQueueSnapshot;
}

type SerialMessageWorker<T> = (item: T) => Promise<boolean>;
type SnapshotListener = (snapshot: SerialMessageQueueSnapshot) => void;

/**
 * Runs queued items one at a time in insertion order.
 *
 * A failed item stays at the front of the queue until retry() is called or the
 * idle queue is cleared. Items added while the queue is paused remain behind
 * that failed item.
 */
export function createSerialMessageQueue<T>(
  worker: SerialMessageWorker<T>,
  onSnapshot?: SnapshotListener,
): SerialMessageQueue<T> {
  const items: T[] = [];
  let active = false;
  let paused = false;

  const getSnapshot = (): SerialMessageQueueSnapshot => ({
    active,
    paused,
    size: items.length,
    waiting: Math.max(0, items.length - (active ? 1 : 0)),
  });

  const emitSnapshot = () => {
    if (!onSnapshot) return;
    try {
      onSnapshot(getSnapshot());
    } catch {
      // Snapshot observers must not interrupt queue processing.
    }
  };

  const runNext = () => {
    if (active || paused || items.length === 0) return;

    active = true;
    const current = items[0];
    emitSnapshot();

    void Promise.resolve()
      .then(() => worker(current))
      .then((succeeded) => {
        active = false;
        if (succeeded) {
          items.shift();
          emitSnapshot();
          runNext();
          return;
        }
        paused = true;
        emitSnapshot();
      })
      .catch(() => {
        active = false;
        paused = true;
        emitSnapshot();
      });
  };

  const enqueue = (item: T) => {
    items.push(item);
    emitSnapshot();
    runNext();
  };

  const retry = () => {
    if (active || !paused || items.length === 0) return false;
    paused = false;
    emitSnapshot();
    runNext();
    return true;
  };

  const clearPending = () => {
    if (active) {
      items.splice(1);
    } else {
      items.splice(0);
      paused = false;
    }
    emitSnapshot();
  };

  return {
    enqueue,
    retry,
    clearPending,
    getSnapshot,
  };
}

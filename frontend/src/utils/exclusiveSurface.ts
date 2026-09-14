export type ExclusiveSurfaceOwner = object;

export interface ExclusiveSurfaceCoordinator {
  claim: (owner: ExclusiveSurfaceOwner, dismiss: () => void) => void;
  release: (owner: ExclusiveSurfaceOwner) => void;
}

/**
 * Coordinates portalled surfaces that must never be visible at the same time.
 * The next owner is recorded before the previous one is dismissed so a dismiss
 * callback can safely release its own stale lease without clearing the new one.
 */
export function createExclusiveSurfaceCoordinator(): ExclusiveSurfaceCoordinator {
  let active: {
    owner: ExclusiveSurfaceOwner;
    dismiss: () => void;
  } | null = null;

  return {
    claim(owner, dismiss) {
      if (active?.owner === owner) {
        active = { owner, dismiss };
        return;
      }

      const previous = active;
      active = { owner, dismiss };
      previous?.dismiss();
    },

    release(owner) {
      if (active?.owner === owner) active = null;
    },
  };
}

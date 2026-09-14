import type { Key } from 'react';

export function createExpandedKeySet(defaultExpandedKeys?: Iterable<Key>) {
  return new Set(defaultExpandedKeys ? Array.from(defaultExpandedKeys) : []);
}

export function toggleExpandedKey(currentKeys: ReadonlySet<Key>, key: Key) {
  const nextKeys = new Set(currentKeys);
  if (nextKeys.has(key)) nextKeys.delete(key);
  else nextKeys.add(key);
  return nextKeys;
}

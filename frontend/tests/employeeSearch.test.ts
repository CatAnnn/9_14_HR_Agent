import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildEmployeeSearchParams,
  filterEmployeeRecords,
  EMPLOYEE_SEARCH_DEBOUNCE_MS,
  shouldCloseEmployeeResults,
  shouldAwaitFreshEmployeeDirectory,
} from '../src/utils/employeeSearch.ts';

test('employee search never applies a result limit', () => {
  const empty = buildEmployeeSearchParams('');
  const fuzzy = buildEmployeeSearchParams('  XU   Nai  ');

  assert.equal(empty.toString(), '');
  assert.equal(empty.has('limit'), false);
  assert.equal(fuzzy.get('q'), 'XU Nai');
  assert.equal(fuzzy.has('limit'), false);
});

test('employee fuzzy search uses a short debounce', () => {
  assert.equal(EMPLOYEE_SEARCH_DEBOUNCE_MS, 80);
});

test('a stale manual cache miss waits for the refreshed employee directory', () => {
  assert.equal(shouldAwaitFreshEmployeeDirectory({
    cacheIsStale: true,
    cachedMatchCount: 0,
    query: 'new employee',
    silent: false,
  }), true);
  assert.equal(shouldAwaitFreshEmployeeDirectory({
    cacheIsStale: true,
    cachedMatchCount: 1,
    query: 'existing employee',
    silent: false,
  }), false);
  assert.equal(shouldAwaitFreshEmployeeDirectory({
    cacheIsStale: true,
    cachedMatchCount: 0,
    query: 'new employee',
    silent: true,
  }), false);
});

test('employee search filters the prefetched directory across fields and terms', () => {
  const records = [
    {
      employee_id: '80000009',
      name: 'Mr. 许乃文/XU Naiwen',
      department: 'ETAS-ICA/xx',
      role: 'Field Application Engineer',
      profile_text: 'Cross Function',
    },
    {
      employee_id: '80000010',
      name: 'Mr. 沈赢枭/SHEN Yingxiao',
      department: 'BEG/xx',
      role: 'Customer Quality Engineer',
      profile_text: 'International',
    },
  ];

  assert.deepEqual(filterEmployeeRecords(records, ''), records);
  assert.deepEqual(filterEmployeeRecords(records, 'xu nai'), [records[0]]);
  assert.deepEqual(filterEmployeeRecords(records, 'ETAS engineer'), [records[0]]);
  assert.deepEqual(filterEmployeeRecords(records, 'cross function'), [records[0]]);
  assert.deepEqual(filterEmployeeRecords(records, '不存在'), []);
});

test('employee results close only for an outside click in the current page', () => {
  assert.equal(shouldCloseEmployeeResults({
    insideLookup: false,
    insideSidebar: false,
  }), true);
  assert.equal(shouldCloseEmployeeResults({
    insideLookup: true,
    insideSidebar: false,
  }), false);
});

test('employee results ignore sidebar navigation clicks', () => {
  assert.equal(shouldCloseEmployeeResults({
    insideLookup: false,
    insideSidebar: true,
  }), false);
});

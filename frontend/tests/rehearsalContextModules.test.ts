import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getRehearsalContextModulePosition,
  REHEARSAL_CONTEXT_MODULES,
} from '../src/utils/rehearsalContextModules.ts';

test('defines the five rehearsal context modules in display order', () => {
  assert.deepEqual(REHEARSAL_CONTEXT_MODULES, [
    { id: 'context', label: '会话信息' },
    { id: 'employee', label: '员工信息' },
    { id: 'goals', label: '员工目标' },
    { id: 'performance', label: '员工表现' },
    { id: 'guidance', label: '谈前指导' },
  ]);
});

test('calculates before, active, and after for every five-by-five combination', () => {
  for (let activeIndex = 0; activeIndex < REHEARSAL_CONTEXT_MODULES.length; activeIndex += 1) {
    for (let index = 0; index < REHEARSAL_CONTEXT_MODULES.length; index += 1) {
      const expected = index < activeIndex
        ? 'before'
        : index === activeIndex
          ? 'active'
          : 'after';
      assert.equal(
        getRehearsalContextModulePosition(index, activeIndex),
        expected,
        'index=' + index + ', activeIndex=' + activeIndex,
      );
    }
  }
});

test('places employee performance before the fifth guidance module', () => {
  const performanceIndex = REHEARSAL_CONTEXT_MODULES.findIndex(
    (module) => module.id === 'performance',
  );
  const guidanceIndex = REHEARSAL_CONTEXT_MODULES.findIndex(
    (module) => module.id === 'guidance',
  );

  assert.equal(
    getRehearsalContextModulePosition(performanceIndex, guidanceIndex),
    'before',
  );
});

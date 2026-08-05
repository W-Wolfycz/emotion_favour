const test = require('node:test');
const assert = require('node:assert/strict');

const {
  chooseInitialPersona,
  filterPersonaOptions,
  nextPersonaIndex,
} = require('../pages/webui/persona_logic.js');

test('initial persona keeps current, then stored, then default', () => {
  const personas = ['persona_saved', 'default', 'persona_demo'];
  assert.equal(
    chooseInitialPersona(personas, 'persona_demo', 'persona_saved'),
    'persona_demo',
  );
  assert.equal(
    chooseInitialPersona(personas, '', 'persona_saved'),
    'persona_saved',
  );
  assert.equal(chooseInitialPersona(personas, '', ''), 'default');
});

test('persona filtering is case insensitive and empty query shows all', () => {
  const personas = ['Alpha', 'beta', 'persona_demo'];
  assert.deepEqual(filterPersonaOptions(personas, ''), personas);
  assert.deepEqual(filterPersonaOptions(personas, 'A'), ['Alpha', 'beta', 'persona_demo']);
  assert.deepEqual(filterPersonaOptions(personas, 'DEMO'), ['persona_demo']);
});

test('keyboard index starts at first or last and wraps', () => {
  assert.equal(nextPersonaIndex(-1, 1, 4), 0);
  assert.equal(nextPersonaIndex(-1, -1, 4), 3);
  assert.equal(nextPersonaIndex(3, 1, 4), 0);
  assert.equal(nextPersonaIndex(0, -1, 4), 3);
  assert.equal(nextPersonaIndex(-1, 1, 0), -1);
});

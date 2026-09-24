// WebUI 人格选择器的纯逻辑。
// 这些选择决定页面读写的 persona_id（数据按人格隔离）：选错不会报错、页面照常渲染，
// 只会让用户在看/改另一个人格的记录，所以留断言盯住。
const test = require('node:test');
const assert = require('node:assert/strict');

const {
  chooseInitialPersona,
  filterPersonaOptions,
  nextPersonaIndex,
} = require('../pages/webui/persona_logic.js');

test('initial persona keeps current, then stored, then default', () => {
  // 守初选优先级：当前会话 → 上次保存 → default。顺序判错会让页面悄悄落到
  // 另一个人格的数据上（下拉框里是名字，不专门核对看不出来）。
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
  // 守搜索过滤：空查询要显示全部（否则下拉框看起来没数据），匹配必须大小写不敏感。
  // 大写查询用两种数据各测一次：'DEMO' 守查询侧归一，'ALPHA' 守选项侧归一（缺一侧就漏匹配）。
  const personas = ['Alpha', 'beta', 'persona_demo'];
  assert.deepEqual(filterPersonaOptions(personas, ''), personas);
  assert.deepEqual(filterPersonaOptions(personas, 'A'), ['Alpha', 'beta', 'persona_demo']);
  assert.deepEqual(filterPersonaOptions(personas, 'DEMO'), ['persona_demo']);
  assert.deepEqual(filterPersonaOptions(personas, 'ALPHA'), ['Alpha']);
});

test('keyboard index starts at first or last and wraps', () => {
  // 守键盘导航：未选中时按方向键从首/末项进入、到底回卷、空列表返回 -1。
  // 取模写错只会让高亮跳错项，回车后选中的是另一个人格。
  assert.equal(nextPersonaIndex(-1, 1, 4), 0);
  assert.equal(nextPersonaIndex(-1, -1, 4), 3);
  assert.equal(nextPersonaIndex(3, 1, 4), 0);
  assert.equal(nextPersonaIndex(0, -1, 4), 3);
  assert.equal(nextPersonaIndex(-1, 1, 0), -1);
});

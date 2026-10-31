// WebUI 人格选择器的纯逻辑。
// 只留初选优先级：它决定页面读写的 persona_id（数据按人格隔离），选错不会报错、
// 页面照常渲染，只会让用户在改另一个人格的记录。搜索过滤与键盘导航是看得见的
// 界面行为（下拉框实时过滤、高亮跳位），不再断言。
const test = require('node:test');
const assert = require('node:assert/strict');

const { chooseInitialPersona } = require('../pages/webui/persona_logic.js');

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

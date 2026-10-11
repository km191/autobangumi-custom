/**
 * order-tab.js 的逻辑测试（不需要浏览器）。
 *
 * 用最小 DOM 垫片把组件跑起来，验证：
 *  - 加载时按 `bangumi_id` 调 `host.api.get('order', …)`，并按返回顺序渲染；
 *  - 下拉框给出正确的位置选项；上移/下移（走同一套 `_move`）改变顺序；
 *  - 保存时以新的顺序 `host.api.put('order', …)`；
 *  - 宿主只提供旧式 `host.request` 时也能工作（向后兼容）；
 *  - 接口报错时状态行给出可读原因。
 *
 * 运行：node test_order_tab.mjs
 */
const calls = [];
const toasts = [];

class FakeClassList {
  constructor() {
    this.set = new Set();
  }
  add(name) {
    this.set.add(name);
  }
  remove(name) {
    this.set.delete(name);
  }
}

class FakeNode {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.classList = new FakeClassList();
    this.listeners = {};
    this.textContent = '';
    this.className = '';
    this.disabled = false;
    this.title = '';
    this.value = '';
  }
  append(...kids) {
    for (const kid of kids) {
      if (kid && typeof kid === 'object') {
        kid.parent = this;
        this.children.push(kid);
      } else {
        this.children.push({ text: String(kid) });
      }
    }
  }
  replaceChildren(...kids) {
    this.children = [];
    this.append(...kids);
  }
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  setAttribute(name, value) {
    (this.attributes ||= {})[name] = String(value);
  }
  getAttribute(name) {
    return (this.attributes || {})[name];
  }
  dispatch(type, event = {}) {
    for (const fn of this.listeners[type] || []) {
      fn({ preventDefault() {}, stopPropagation() {}, target: this, ...event });
    }
  }
  attachShadow() {
    this.shadowRoot = new FakeNode('#shadow');
    return this.shadowRoot;
  }
}

globalThis.HTMLElement = FakeNode;
globalThis.document = { createElement: (tag) => new FakeNode(tag) };
globalThis.customElements = {
  registry: new Map(),
  define(name, cls) {
    this.registry.set(name, cls);
  },
  get(name) {
    return this.registry.get(name);
  },
};

const failures = [];
function check(label, got, expected) {
  const ok = JSON.stringify(got) === JSON.stringify(expected);
  console.log(`  [${ok ? 'OK ' : 'FAIL'}] ${label}: ${JSON.stringify(got)}`);
  if (!ok) {
    console.log(`         期望: ${JSON.stringify(expected)}`);
    failures.push(label);
  }
}

const RULES = [
  { id: 11, group_name: 'ANi', tags: '1080P · CHT · Baha' },
  { id: 12, group_name: '喵萌奶茶屋&LoliHouse', tags: '1080p · 简繁内封字幕 · WebRip' },
];

await import('./web/order-tab.js');

const ElementClass = customElements.get('ab-plugin-quality-wash-order');
check('组件已注册', typeof ElementClass, 'function');

function makeElement(host) {
  const el = new ElementClass();
  el.host = host;
  el.context = { bangumiId: 2 };
  el._loaded = true; // 阻止 setter 触发的自动加载，改为手动驱动
  el.connectedCallback();
  return el;
}

const okHost = {
  pluginId: 'quality-wash',
  api: {
    get: async (path, params) => {
      calls.push({ method: 'get', path, params });
      return { anime: '花织即使是转生也想打架', rules: RULES, rule_ids: [11, 12] };
    },
    put: async (path, body) => {
      calls.push({ method: 'put', path, body });
      return { ok: true };
    },
  },
  toast: (text) => toasts.push(text),
};

const el = makeElement(okHost);
await el._load();

const wrap = el.shadowRoot.children[1];
const list = wrap.children.find((node) => node.tagName === 'ul');
check('加载后顺序', el._order, [11, 12]);
check('GET 用的是 host.api.get', calls[0], {
  method: 'get',
  path: 'order',
  params: { bangumi_id: 2 },
});
check('渲染出的行数', list.children.length, 2);
check('首行是执行凭证', wrap.children[0].textContent.includes('quality-wash'), true);

const firstRow = list.children[0];
const select = firstRow.children[0];
check('每行有顺序下拉框', select.tagName, 'select');
check('下拉框选项数 = 规则数', select.children.length, 2);
check('第一行默认选中第 1 位', select.children[0].textContent, '1');
check('第一行显示组名', firstRow.children[1].children[0].textContent, 'ANi');

// 下拉框切到第 2 位
select.value = '1';
select.dispatch('change');
check('下拉框换位后顺序', el._order, [12, 11]);
check('提示有改动', el._status.includes('保存'), true);

await el._save();
const put = calls.find((c) => c.method === 'put');
check('PUT 的是新顺序', put.body, { bangumi_id: 2, rule_ids: [12, 11] });
check('保存后有反馈', toasts.length > 0, true);
check('保存成功状态', el._status.includes('已保存'), true);

// 旧的 host.request 形态也能用
const legacyCalls = [];
const legacy = makeElement({
  request: async (method, path, options) => {
    legacyCalls.push({ method, path, options });
    // 接口按保存顺序返回 rules，组件就按这个顺序建列表
    return { rules: [...RULES].reverse(), rule_ids: [12, 11] };
  },
});
await legacy._load();
check('旧式宿主也能读到规则（按接口顺序）', legacy._order, [12, 11]);
check('旧式宿主调用形态', legacyCalls[0], {
  method: 'get',
  path: 'order',
  options: { params: { bangumi_id: 2 } },
});

// 接口报错 → 状态行可读
const failing = makeElement({
  api: {
    get: async () => {
      const error = new Error('boom');
      error.response = { data: { detail: '规则清单已变化，请刷新页面后重试' } };
      throw error;
    },
  },
});
await failing._load();
check('错误原因可读', failing._status, '读取失败：规则清单已变化，请刷新页面后重试');

// 宿主既没有 api 也没有 request → 明确报错
const broken = makeElement({ pluginId: 'quality-wash' });
await broken._load();
check('缺 api 时报错可读', broken._status.startsWith('读取失败：宿主未提供'), true);

// 新订阅待确认：显示暂停提示 + 「立即放行」，点击后 POST order/release 并刷新
let released = false;
const heldCalls = [];
const heldHost = {
  pluginId: 'quality-wash',
  api: {
    get: async (path, params) => {
      heldCalls.push({ method: 'get', path, params });
      return {
        anime: '某新番',
        rules: RULES,
        rule_ids: [11, 12],
        held: !released,
        hold_enabled: true,
      };
    },
    put: async (path, body) => {
      heldCalls.push({ method: 'put', path, body });
      return { ok: true };
    },
    post: async (path, body) => {
      heldCalls.push({ method: 'post', path, body });
      released = true;
      return { ok: true, held: false };
    },
  },
  toast: (text) => toasts.push(text),
};

const held = makeElement(heldHost);
await held._load();
const heldWrap = held.shadowRoot.children[1];
check('待确认写进执行凭证', heldWrap.children[0].textContent.includes('待确认'), true);
check(
  '有暂停提示条',
  heldWrap.children.some((n) => n.getAttribute && n.getAttribute('data-held') === 'true'),
  true
);
const heldBar = heldWrap.children[heldWrap.children.length - 1];
const releaseBtn = heldBar.children.find((n) => n.textContent === '立即放行');
check('有「立即放行」按钮', !!releaseBtn, true);

await held._release();
const posted = heldCalls.find((c) => c.method === 'post');
check('放行调的是 POST order/release', posted, {
  method: 'post',
  path: 'order/release',
  body: { bangumi_id: 2 },
});
check('放行后不再待确认', held._held, false);
check('放行后提示消失', held.shadowRoot.children[1].textContent.includes('立即放行'), false);


// v0.6：标签用「实际收到的字幕组」，AB 记错的组作为提示
{
  const actualHost = {
    pluginId: 'quality-wash',
    api: {
      get: async () => ({
        anime: '至高之力',
        rules: [
          { id: 8, group_name: 'ANi', actual_groups: ['LoliHouse'], tags: '1080P · CHT' },
          { id: 94, group_name: 'ANi', actual_groups: [], tags: '1080P · CHT' },
        ],
        rule_ids: [8, 94],
        held: false,
      }),
      put: async () => ({ ok: true }),
    },
    toast: () => {},
  };
  const el2 = makeElement(actualHost);
  await el2._load();
  const wrap2 = el2.shadowRoot.children[1];
  const list2 = wrap2.children.find((n) => n.tagName === 'ul');
  check('实际组作为主标签', list2.children[0].children[1].children[0].textContent, 'LoliHouse');
  check(
    'AB 记错的组作为提示',
    list2.children[0].children[1].children[1].textContent.includes('AB 记的是 ANi'),
    true
  );
  check(
    '还没抓到种子时给出说明',
    list2.children[1].children[1].children[0].textContent,
    'ANi'
  );
  check(
    '并提示待定',
    list2.children[1].children[1].children[1].textContent.includes('字幕组待定'),
    true
  );
}

console.log();
console.log('='.repeat(70));
if (failures.length) {
  console.log(`结果：${failures.length} 项失败`);
  for (const item of failures) console.log('  -', item);
  process.exit(1);
}
console.log('结果：全部通过');

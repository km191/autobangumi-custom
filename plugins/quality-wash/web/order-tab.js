/**
 * quality-wash 的前端组件：在同一部番的「编辑规则」弹窗里设置各字幕组规则的
 * 下载顺序（快的在上、优质补档在下）。
 *
 * 挂载点：`bangumi.detail.tab`，宿主注入：
 *   element.context = { bangumiId: <规则 id> }
 *   element.host    = {
 *     pluginId,
 *     api: { get(path, params?), post(path, body?), put(path, body?), delete(path) },
 *     i18n: { locale, t }, theme: { mode, tokens },
 *     toast(message, kind), events: { on(kind, cb) },
 *   }        ← 注意是 host.api.get / host.api.put，不是 host.request
 *
 * 相对路径（如 `order`）由宿主解析到 `/api/v1/plugins/quality-wash/` 并带上会话。
 * 顺序用**下拉框**选择（1 = 先取），不做拖拽 —— 弹窗里更稳、也更好点。
 *
 * 纯 vanilla（无构建步骤），元素名必须以 ab-plugin-<插件 id> 开头。
 */
const ELEMENT = 'ab-plugin-quality-wash-order';
/** 组件版本，会显示在面板首行，用来判断组件到底有没有跑起来 */
const VERSION = 'v0.6';

const STYLE = `
  :host { display:block; font-family: var(--ab-font-family, system-ui); color: var(--ab-color-text, #1e293b); font-size:13px; }
  .wrap { display:flex; flex-direction:column; gap:10px; }
  .meta { color: var(--ab-color-text-muted, #94a3b8); font-size:12px; }
  .hint { color: var(--ab-color-text-secondary, #64748b); line-height:1.6; }
  .list { display:flex; flex-direction:column; gap:6px; margin:0; padding:0; list-style:none; }
  .row { display:flex; align-items:center; gap:8px; padding:8px 10px;
         border:1px solid var(--ab-color-border, #e2e8f0); border-radius: var(--ab-radius-md, 8px);
         background: var(--ab-color-surface, #fff); }
  select { font: inherit; padding:3px 6px; border-radius: var(--ab-radius-sm, 4px);
           border:1px solid var(--ab-color-border, #e2e8f0); background: var(--ab-color-surface, #fff); color: inherit; }
  .grow { flex:1; min-width:0; }
  .group { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .tags { color: var(--ab-color-text-secondary, #64748b); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  button { font: inherit; padding:4px 10px; border-radius: var(--ab-radius-sm, 4px);
           border:1px solid var(--ab-color-border, #e2e8f0); background: var(--ab-color-surface, #fff);
           color: inherit; cursor:pointer; }
  button.primary { background: var(--ab-color-primary, #6c4ab6); border-color: var(--ab-color-primary, #6c4ab6); color:#fff; }
  button:disabled { opacity:.5; cursor:default; }
  .bar { display:flex; align-items:center; gap:8px; }
  .status { color: var(--ab-color-text-secondary, #64748b); font-size:12px; flex:1; }
  .err { color: var(--ab-color-danger, #ef4444); }
`;

class OrderTab extends HTMLElement {
  constructor() {
    super();
    this._host = null;
    this._context = {};
    this._rules = [];
    this._order = [];
    this._status = '';
    this._error = false;
    this._loaded = false;
    this._wrap = null;
    this._held = false;
    this._holdEnabled = false;
  }

  /** 宿主注入：{ bangumiId } */
  set context(value) {
    this._context = value || {};
    this._maybeLoad();
  }

  get context() {
    return this._context;
  }

  /** 宿主注入：api / toast / i18n / theme / events */
  set host(value) {
    this._host = value || null;
    this._maybeLoad();
  }

  get host() {
    return this._host;
  }

  connectedCallback() {
    this._ensureRoot();
    this._render(); // 先渲染出可见内容，避免"空白面板"
    this._maybeLoad();
  }

  /** 影子根只建一次；任何需要渲染的地方都会先确保它存在 */
  _ensureRoot() {
    if (this.shadowRoot) return this.shadowRoot;
    const root = this.attachShadow({ mode: 'open' });
    const style = document.createElement('style');
    style.textContent = STYLE;
    const wrap = document.createElement('div');
    wrap.className = 'wrap';
    root.append(style, wrap);
    this._wrap = wrap;
    return root;
  }

  _maybeLoad() {
    if (this._loaded || !this._host || !this._context.bangumiId) return;
    this._loaded = true;
    console.log('[quality-wash] 开始加载下载顺序', VERSION, this._context);
    this._load();
  }

  /** 兼容两种宿主形态：host.api.get（4.0 正式）与 host.request（早期草案） */
  _api() {
    const host = this._host;
    if (host?.api?.get) return host.api;
    if (host?.request) {
      return {
        get: (path, params) => host.request('get', path, { params }),
        put: (path, body) => host.request('put', path, { data: body }),
      };
    }
    throw new Error('宿主未提供 api（host.api.get 不存在）');
  }

  async _load() {
    this._setStatus('加载中…');
    try {
      const api = this._api();
      const data = await api.get('order', { bangumi_id: this._context.bangumiId });
      this._rules = data?.rules || [];
      this._order = this._rules.map((rule) => rule.id);
      this._held = !!data?.held;
      this._holdEnabled = !!data?.hold_enabled;
      this._setStatus('');
      console.log('[quality-wash] 已读取', this._order.length, '条规则');
    } catch (error) {
      console.error('[quality-wash] 读取下载顺序失败', error);
      this._setStatus('读取失败：' + this._reason(error), true);
    }
    this._render();
  }

  async _release() {
    const api = this._api();
    if (!api) return;
    if (this._busy) return;
    this._busy = true;
    try {
      await api.post('order/release', {
        bangumi_id: this._context?.bangumiId,
      });
      this._setStatus('已放行，按当前顺序开始下载 ✓');
      await this._load();
    } catch (e) {
      this._setStatus(`放行失败：${e?.message || e}`, true);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _save() {
    this._setStatus('保存中…');
    try {
      const api = this._api();
      await api.put('order', {
        bangumi_id: this._context.bangumiId,
        rule_ids: this._order,
      });
      this._setStatus('已保存 ✓');
      this._host?.toast?.('下载顺序已保存', 'info');
      console.log('[quality-wash] 已保存顺序', this._order);
    } catch (error) {
      console.error('[quality-wash] 保存下载顺序失败', error);
      this._setStatus('保存失败：' + this._reason(error), true);
    }
    this._render();
  }

  _reason(error) {
    const detail = error?.response?.data?.detail;
    if (typeof detail === 'string') return detail;
    return error?.message || String(error);
  }

  _setStatus(text, isError = false) {
    this._status = text;
    this._error = isError;
    this._render();
  }

  /** 把第 from 条挪到第 to 位（0 基） */
  _move(from, to) {
    if (!Number.isInteger(from) || !Number.isInteger(to)) return;
    if (from < 0 || from >= this._order.length) return;
    if (to < 0 || to >= this._order.length || from === to) return;
    const [item] = this._order.splice(from, 1);
    this._order.splice(to, 0, item);
    this._setStatus('有改动，记得保存');
  }

  _byId(id) {
    return (
      this._rules.find((rule) => rule.id === id) || {
        id,
        group_name: `规则 #${id}`,
        tags: '',
      }
    );
  }

  _row(id, index) {
    const rule = this._byId(id);
    const row = document.createElement('li');
    row.className = 'row';

    // 顺序选择：下拉框（1 = 先取）
    const select = document.createElement('select');
    select.title = '选择下载顺序';
    select.setAttribute('aria-label', `规则 ${rule.group_name} 的顺序`);
    for (let i = 0; i < this._order.length; i += 1) {
      const option = document.createElement('option');
      option.value = String(i);
      option.textContent = `${i + 1}`;
      if (i === index) option.selected = true;
      select.append(option);
    }
    select.addEventListener('change', () => {
      this._move(index, Number(select.value));
    });

    const grow = document.createElement('div');
    grow.className = 'grow';
    const group = document.createElement('div');
    group.className = 'group';
    // 优先显示"实际收到的字幕组"（从 qB 的 ab:<id> 标签统计），AB 那个字段只做提示
    const actual = Array.isArray(rule.actual_groups) ? rule.actual_groups : [];
    group.textContent = actual.length
      ? actual.join(' / ')
      : rule.group_name || `规则 #${id}`;
    const tags = document.createElement('div');
    tags.className = 'tags';
    const parts = [];
    if (rule.tags) parts.push(rule.tags);
    if (actual.length && rule.group_name && !actual.includes(rule.group_name)) {
      parts.push(`（AB 记的是 ${rule.group_name}）`);
    }
    if (!actual.length) {
      parts.push('（还没抓到种子，字幕组待定）');
    }
    tags.textContent = parts.join('  ');
    grow.append(group, tags);

    const up = document.createElement('button');
    up.textContent = '↑';
    up.title = '上移';
    up.disabled = index === 0;
    up.addEventListener('click', () => this._move(index, index - 1));

    const down = document.createElement('button');
    down.textContent = '↓';
    down.title = '下移';
    down.disabled = index === this._order.length - 1;
    down.addEventListener('click', () => this._move(index, index + 1));

    row.append(select, grow, up, down);
    return row;
  }

  _render() {
    this._ensureRoot();
    if (!this._wrap) return;
    this._wrap.replaceChildren();

    // 首行是执行凭证：只要面板不是空的，就说明组件确实跑起来了
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent =
      `quality-wash ${VERSION} ｜ 规则 #${this._context?.bangumiId ?? '?'} ｜ ` +
      `已读 ${this._order.length} 条规则` +
      (this._held ? ' ｜ ⏸ 待确认' : '');
    this._wrap.append(meta);

    const hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent =
      '用下拉框选择先后顺序（1 = 先取，快的组放前面、优质补档放后面），' +
      '也可以用 ↑ ↓ 微调。同一档位时按这个顺序先取；最终保留哪一份仍由' +
      '「优质组 / 垫底组」质量清单决定。';
    this._wrap.append(hint);

    if (this._held) {
      const banner = document.createElement('div');
      banner.className = 'hint held';
      banner.setAttribute('data-held', 'true');
      banner.textContent =
        '⏸ 这个新订阅已暂停，确认前不会下载。排好字幕组顺序后点「保存顺序」会自动放行；' +
        '想直接用默认顺序就点「立即放行」。';
      this._wrap.append(banner);
    } else if (this._holdEnabled) {
      const ok = document.createElement('div');
      ok.className = 'hint';
      ok.textContent = '（已开启「新订阅先暂停」：这条规则已确认，正常下载）';
      this._wrap.append(ok);
    }

    const list = document.createElement('ul');
    list.className = 'list';
    this._order.forEach((id, index) => list.append(this._row(id, index)));
    this._wrap.append(list);

    const bar = document.createElement('div');
    bar.className = 'bar';
    if (this._held) {
      const release = document.createElement('button');
      release.className = 'primary';
      release.textContent = '立即放行';
      release.title = '不调整顺序，直接用默认档位开始下载';
      release.addEventListener('click', () => this._release());
      bar.append(release);
    }
    const save = document.createElement('button');
    save.className = 'primary';
    save.textContent = '保存顺序';
    save.addEventListener('click', () => this._save());
    const status = document.createElement('span');
    status.className = this._error ? 'status err' : 'status';
    status.textContent = this._status;
    bar.append(save, status);
    this._wrap.append(bar);
  }
}

if (!customElements.get(ELEMENT)) {
  customElements.define(ELEMENT, OrderTab);
}

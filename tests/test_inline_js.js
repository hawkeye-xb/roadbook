/* 无第三方依赖：用最小 DOM 执行生成的内联脚本，覆盖 Python 测不到的交互。 */
'use strict';

const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const scripts = JSON.parse(execFileSync('python3', ['-I', '-c', `
import json, re, runpy
ns = runpy.run_path(${JSON.stringify(path.join(root, 'scripts', 'render_html.py'))})
print(json.dumps(re.findall(r'<script>(.*?)</script>', ns['TEMPLATE'], re.S)))
`], { encoding: 'utf8', cwd: root }));
const accordionScript = scripts[0];
// 地图与导航在同一个 script 标签内，但导航是独立 IIFE；只执行导航段，不引入 Leaflet。
const navScript = '// 一键导航' + scripts[scripts.length - 1].split('// 一键导航')[1];

class ClassList {
  constructor(owner, value = '') {
    this.owner = owner;
    this.values = new Set(value.split(/\s+/).filter(Boolean));
  }
  add(...names) { names.forEach(name => this.values.add(name)); }
  remove(...names) { names.forEach(name => this.values.delete(name)); }
  contains(name) { return this.values.has(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.contains(name) : Boolean(force);
    if (on) this.add(name); else this.remove(name);
    return on;
  }
}

class Element {
  constructor(tag = 'div', classes = '') {
    this.tagName = tag.toUpperCase();
    this.classList = new ClassList(this, classes);
    this.attributes = {};
    this.listeners = {};
    this.textContent = '';
    this.children = [];
    this.parentNode = null;
    this.nextElementSibling = null;
    this.nextSibling = null;
    this.id = '';
  }
  set className(value) { this.classList = new ClassList(this, value); }
  get className() { return [...this.classList.values].join(' '); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return Object.hasOwn(this.attributes, name) ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter(item => item !== fn);
  }
  fire(type) { (this.listeners[type] || []).slice().forEach(fn => fn({ target: this })); }
  click() {
    const event = { target: this, preventDefault() {}, stopPropagation() {} };
    (this.listeners.click || []).slice().forEach(fn => fn(event));
  }
  closest(selector) {
    if (selector === '.day-card') {
      let node = this;
      while (node) {
        if (node.classList.contains('day-card')) return node;
        node = node.parentNode;
      }
    }
    return null;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    if (selector === '.day-head') return this.head ? [this.head] : [];
    if (selector === '.day-date') return this.date ? [this.date] : [];
    if (selector === '.day-more') return this.more ? [this.more] : [];
    return [];
  }
  scrollIntoView() {}
  insertBefore(el) {
    el.parentNode = this;
    this.children.push(el);
    if (this.nav) this.nav.nextElementSibling = this.nav.nextSibling = el;
    return el;
  }
}

class Document {
  constructor(cards, navs) {
    this.cards = cards;
    this.navs = navs;
    this.listeners = {};
    this.hidden = false;
    this.documentElement = new Element('html');
  }
  querySelectorAll(selector) {
    if (selector === '.day-card') return this.cards;
    if (selector === '.day-card .day-head') return this.cards.map(card => card.head);
    if (selector === '.day-more') return this.cards.map(card => card.more).filter(Boolean);
    if (selector === '.nav-btn') return this.navs;
    if (selector === '.day-card.open') return this.cards.filter(card => card.classList.contains('open'));
    if (selector === '.day-card.active') return this.cards.filter(card => card.classList.contains('active'));
    return [];
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getElementById(id) { return this.cards.find(card => card.id === id) || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter(item => item !== fn);
  }
  fire(type) { (this.listeners[type] || []).slice().forEach(fn => fn({ target: this })); }
  createElement(tag) { return new Element(tag); }
}

function makeCard(index, date = '') {
  const card = new Element('section', 'day-card');
  card.id = `day-${index}`;
  card.setAttribute('data-day', index);
  card.head = new Element('header', 'day-head');
  card.head.parentNode = card;
  card.date = new Element('span', 'day-date');
  card.date.textContent = date;
  card.more = new Element('details', 'day-more');
  card.more.setAttribute('open', '');
  card.nav = new Element('a', 'nav-btn');
  card.nav.parentNode = card;
  card.nav.setAttribute('data-ios', `current-${index}`);
  card.nav.setAttribute('data-ios-fixed', `fixed-${index}`);
  card.nav.setAttribute('data-android', `current-${index}`);
  card.nav.setAttribute('data-android-fixed', `fixed-${index}`);
  card.nav.setAttribute('data-date', date);
  return card;
}

function harness(dates = ['2026-09-25', '2026-09-26', '2026-09-27']) {
  const cards = dates.map((date, index) => makeCard(index + 1, date));
  const navs = cards.map(card => card.nav);
  const document = new Document(cards, navs);
  const mediaListeners = [];
  const windowListeners = {};
  const mq = {
    matches: true,
    addEventListener(type, fn) { if (type === 'change') mediaListeners.push(fn); },
  };
  const window = {
    matchMedia: () => mq,
    addEventListener(type, fn) { (windowListeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) {
      windowListeners[type] = (windowListeners[type] || []).filter(item => item !== fn);
    },
  };
  const timers = [];
  const location = { href: '' };
  const context = {
    window, document, location,
    navigator: { userAgent: 'iPhone', maxTouchPoints: 1 },
    setTimeout(fn, delay) { timers.push({ fn, delay }); return timers.length; },
    clearTimeout() {},
    console,
  };
  vm.createContext(context);
  vm.runInContext(accordionScript, context);
  vm.runInContext(navScript, context);
  return {
    cards, navs, document, window, location, timers,
    setMobile(value) {
      mq.matches = value;
      mediaListeners.slice().forEach(fn => fn());
    },
    fireVisibility() { document.fire('visibilitychange'); },
    firePageHide() { (windowListeners.pagehide || []).slice().forEach(fn => fn({})); },
  };
}

function localToday() {
  const now = new Date();
  const pad = value => String(value).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

test('断点往返后展开当前 active 日，而不是旧 open 日', () => {
  const h = harness();
  h.cards[0].classList.add('active');
  h.setMobile(false);
  h.cards.forEach(card => card.classList.remove('active'));
  h.cards[2].classList.add('active');
  h.setMobile(true);
  assert.deepEqual(h.cards.map(card => card.classList.contains('open')), [false, false, true]);
  assert.equal(h.cards[2].head.getAttribute('aria-expanded'), 'true');
});

test('点击时今天用当前位置，其他日期用固定起点，无日期用当前位置', () => {
  const today = localToday();
  const cases = [
    [today, 'current-1'],
    ['2026-09-27', 'fixed-1'],
    ['', 'current-1'],
  ];
  for (const [date, expected] of cases) {
    const h = harness([date]);
    h.navs[0].click();
    assert.equal(h.location.href, expected, `date=${JSON.stringify(date)}`);
  }
});

test('页面曾切到后台后不在返回时误报未唤起', () => {
  const h = harness(['2026-09-27']);
  h.navs[0].click();
  h.document.hidden = true;
  h.fireVisibility();
  h.document.hidden = false;
  h.fireVisibility();
  h.timers[0].fn();
  assert.equal(h.navs[0].nextElementSibling, null);
});

test('页面始终在前台时保留未安装高德提示', () => {
  const h = harness(['2026-09-27']);
  h.navs[0].click();
  h.timers[0].fn();
  assert.match(h.navs[0].nextElementSibling.textContent, /没有唤起高德地图/);
});

test('pagehide 同样取消未唤起提示', () => {
  const h = harness(['2026-09-27']);
  h.navs[0].click();
  h.firePageHide();
  h.document.hidden = false;
  h.timers[0].fn();
  assert.equal(h.navs[0].nextElementSibling, null);
});

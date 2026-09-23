// Dependency-free UI state regression checks: node tests/test_rewards_ui.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
const rewardSource = fs.readFileSync(path.join(root, 'static/rewards-ui.js'), 'utf8');

function harness() {
  let listener;
  const elements = new Map();
  const document = {activeElement: null, addEventListener: (_, fn) => listener = fn,
    querySelector: selector => elements.get(selector)};
  class Element {
    constructor(selector, parent = null) {
      this.selector = selector; this.parent = parent; this.dataset = {}; this.children = [];
      this.id = selector.startsWith('#') ? selector.slice(1) : '';
      elements.set(selector, this);
    }
    contains(element) {return element === this || this.children.some(child => child.contains(element));}
    querySelector(selector) {const element = elements.get(selector); return this.contains(element) ? element : null;}
    focus() {if (!this.disabled) document.activeElement = this;}
    set innerHTML(html) {
      this.html = html;
      if (this.children.some(child => child.contains(document.activeElement))) document.activeElement = body;
      const remove = element => {element.children.forEach(remove); elements.delete(element.selector);};
      this.children.forEach(remove); this.children = [];
      for (const tag of html.matchAll(/<(?:div|p|button)\b[^>]*>/g)) {
        const id = tag[0].match(/\bid="([^"]+)"/);
        const data = tag[0].match(/data-quest-(buy|goal)="([^"]+)"/);
        if (!id && !data) continue;
        const selector = id ? '#' + id[1] : `[data-quest-${data[1]}="${data[2]}"]`;
        const element = new Element(selector, this);
        if (data) element.dataset[data[1] === 'buy' ? 'questBuy' : 'questGoal'] = data[2];
        element.disabled = /\sdisabled(?:\s|>)/.test(tag[0]);
        this.children.push(element);
      }
    }
  }
  const body = new Element('body'), panel = new Element('#rewards-panel', body);
  document.activeElement = body;
  const wallet = {balance: 500, earned: 500, spent: 0, goal: null, codes: [], awards: [],
    catalog: [{id: 'coffee', cost: 150, icon: 'x', name: {ru: 'Кофе', kk: 'Кофе', en: 'Coffee'},
      offer: {ru: 'Напиток', kk: 'Сусын', en: 'Drink'}}]};
  const context = vm.createContext({document, $: id => elements.get('#' + id), escapeHtml: String,
    state: {auth: {role: 'employee'}, role: 'employee', lang: 'ru', demo: {employee_id: 'E0001'},
      profile: {completed_activities: []}, rewards: wallet}, crypto: {randomUUID: () => 'request'},
    fetchJson: async () => ({...wallet, balance: 350, spent: 150}),
    localStorage: {getItem: () => null}, setLanguage() {}, render() {}, AbortController,
    setTimeout, clearTimeout, requestVersion: 0, recommendationController: null});
  vm.runInContext(rewardSource, context);
  vm.runInContext(source.slice(source.indexOf('async function reload(){'), source.indexOf('\nfunction showView(){')), context);
  vm.runInContext('renderRewards()', context);
  return {context, wallet, document, elements, run: code => vm.runInContext(code, context),
    async click(selector) {const element = elements.get(selector); assert.ok(element, selector);
      element.focus(); return listener({target: {closest: () => element}});}};
}

(async () => {
  const h = harness();
  assert.equal(h.run('Object.values(QUEST_COPY).every(copy=>Object.keys(QUEST_COPY.ru).every(key=>copy[key]))'), true);
  await h.click('[data-quest-buy="coffee"]');
  assert.equal(h.document.activeElement.id, 'quest-confirm');
  await h.click('#quest-cancel');
  assert.equal(h.document.activeElement.dataset.questBuy, 'coffee');
  const status = h.elements.get('#quest-status');
  await h.click('[data-quest-buy="coffee"]');
  await h.click('#quest-confirm');
  assert.equal(h.document.activeElement.id, 'quest-status');
  assert.equal(h.elements.get('#quest-status'), status, 'live region survives renders');
  assert.match(status.textContent, /Демо-промокод выдан/);

  // A delayed profile-load wallet must not undo a completed redemption.
  const race = harness();
  let release;
  race.context.fetchJson = async url => url === '/api/rewards' ? new Promise(resolve => release = resolve)
    : url.startsWith('/api/profile') ? {employee: {preferred_language: 'ru'}}
    : url === '/api/rewards/redeem' ? {...race.wallet, balance: 350, spent: 150} : {};
  const reload = race.run('reload()');
  await race.click('[data-quest-buy="coffee"]');
  await race.click('#quest-confirm');
  release(race.wallet); await reload;
  assert.equal(race.context.state.rewards.balance, 350);

  // A GET begun during a mutation is stale too, even if it returns last.
  const during = harness();
  let releaseMutation, releaseRead;
  during.context.fetchJson = async url => url === '/api/rewards' ? new Promise(resolve => releaseRead = resolve)
    : url === '/api/rewards/redeem' ? new Promise(resolve => releaseMutation = resolve)
    : url.startsWith('/api/profile') ? {employee: {preferred_language: 'ru'}} : {};
  await during.click('[data-quest-buy="coffee"]');
  const mutation = during.click('#quest-confirm');
  const pendingReload = during.run('reload()');
  releaseMutation({...during.wallet, balance: 350, spent: 150}); await mutation;
  releaseRead(during.wallet); await pendingReload;
  assert.equal(during.context.state.rewards.balance, 350);
  console.log('PASS: translations, cancel focus, redemption focus, persistent live region, two stale-wallet races');
})().catch(error => {console.error(error); process.exitCode = 1;});

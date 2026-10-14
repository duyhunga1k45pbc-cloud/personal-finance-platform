'use strict';

// Run from the PFP backend directory:
//   node --test tests/test_oidc_session_lifecycle.cjs
// No network or real Keycloak credentials are used.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const moneta = path.resolve(__dirname, '..', 'static', 'moneta.html');
const html = fs.readFileSync(moneta, 'utf8');

// Extract the real production functions, rather than maintaining copies of
// the refresh/logout implementation in tests. The selected functions contain
// no template literals, so the small balanced-brace scanner is sufficient.
function extractFunction(name) {
  const pattern = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const match = pattern.exec(html);
  if (!match) throw new Error('Missing Moneta function: ' + name);
  const start = match.index;
  const brace = html.indexOf('{', html.indexOf(')', start + match[0].length) + 1);
  if (brace < 0) throw new Error('Missing function body: ' + name);

  let depth = 0;
  let quote = null;
  let lineComment = false;
  let blockComment = false;
  for (let i = brace; i < html.length; i++) {
    const c = html[i];
    const next = html[i + 1];
    if (lineComment) { if (c === '\n') lineComment = false; continue; }
    if (blockComment) { if (c === '*' && next === '/') { blockComment = false; i++; } continue; }
    if (quote) {
      if (c === '\\') { i++; continue; }
      if (c === quote) quote = null;
      continue;
    }
    if (c === '/' && next === '/') { lineComment = true; i++; continue; }
    if (c === '/' && next === '*') { blockComment = true; i++; continue; }
    if (c === '"' || c === "'" || c === '`') { quote = c; continue; }
    if (c === '{') depth++;
    if (c === '}') {
      depth--;
      if (depth === 0) return html.slice(start, i + 1);
    }
  }
  throw new Error('Unterminated Moneta function: ' + name);
}

const functions = [
  'logout',
  'signOutWithKeycloak',
  'ensureFreshOidcAccessToken',
  'api',
].map(extractFunction).join('\n\n');

function makeEnvironment(fetchImpl) {
  const requests = [];
  const navigations = [];
  const storageRemovals = [];
  const elements = new Map();
  const storage = {
    removeItem: (key) => storageRemovals.push(key),
  };
  const element = (id) => {
    if (!elements.has(id)) {
      elements.set(id, {
        innerHTML: '', textContent: '', value: '',
        classList: {add() {}, remove() {}},
      });
    }
    return elements.get(id);
  };
  const ctx = vm.createContext({
    URL, URLSearchParams, AbortController,
    setTimeout, clearTimeout, Date,
    window: {location: {assign: (url) => navigations.push(url)}},
    sessionStorage: storage,
    console: {log() {}, error() {}},
    fetch: async (url, opts = {}) => {
      requests.push({url: String(url), opts});
      return fetchImpl(String(url), opts);
    },
    document: {getElementById: element},
  });
  const declarations = `
    'use strict';
    let token = null;
    let authSource = null;
    let oidcIdToken = null;
    let oidcRefreshToken = null;
    let oidcAccessExpiresAt = 0;
    let oidcRefreshPromise = null;
    let oidcSessionGeneration = 0;
    let accounts = [];
    let transactions = [];
    let summary = null;
    let balances = null;
    const OIDC = {
      issuer: 'http://localhost:8081/realms/pfp-dev',
      clientId: 'moneta',
      redirectUri: 'http://127.0.0.1:8765/demo',
    };
    const OIDC_PENDING = 'moneta.oidc.pkce.v1';
    const $ = id => document.getElementById(id);
    class ApiError extends Error {
      constructor(status, detail) { super(detail); this.status = status; }
    }
  `;
  vm.runInContext(declarations + '\n' + functions, ctx, {timeout: 2000});
  return {
    ctx, requests, navigations, storageRemovals,
    get: (expr) => vm.runInContext(expr, ctx),
    exec: (script) => vm.runInContext(script, ctx),
  };
}

function beginKeycloak(env) {
  env.exec(`
    token = 'access-1';
    authSource = 'keycloak';
    oidcIdToken = 'id-1';
    oidcRefreshToken = 'refresh-1';
    oidcAccessExpiresAt = Date.now() + 1000;
    oidcSessionGeneration += 1;
  `);
}

function success(body) {
  return {ok: true, status: 200, json: async () => body};
}

function authCallCount(env) {
  return env.requests.filter((r) => r.url.endsWith('/protocol/openid-connect/token')).length;
}

function apiRequests(env) {
  return env.requests.filter((r) => !r.url.endsWith('/protocol/openid-connect/token'));
}

test('single-flight: 3 concurrent API calls share one refresh and new bearer', async () => {
  const env = makeEnvironment(async (url) => {
    if (url.endsWith('/token')) {
      await new Promise((r) => setTimeout(r, 10));
      return success({access_token: 'access-2', refresh_token: 'refresh-2', expires_in: 300, id_token: 'id-2'});
    }
    return success({ok: true});
  });
  beginKeycloak(env);
  await env.exec(`Promise.all([api('/auth/me'), api('/accounts'), api('/summary')])`);
  assert.equal(authCallCount(env), 1);
  assert.equal(apiRequests(env).length, 3);
  assert.ok(apiRequests(env).every((r) => r.opts.headers.Authorization === 'Bearer access-2'));
  assert.equal(env.get('oidcRefreshToken'), 'refresh-2');
  assert.equal(env.get('oidcIdToken'), 'id-2');
});

test('refresh token rotation: second exchange uses the newly issued refresh token', async () => {
  let serial = 1;
  const env = makeEnvironment(async (url) => {
    if (url.endsWith('/token')) {
      serial++;
      return success({access_token: 'access-' + serial, refresh_token: 'refresh-' + serial, expires_in: 300});
    }
    return success({ok: true});
  });
  beginKeycloak(env);
  await env.exec(`api('/accounts')`);
  env.exec(`oidcAccessExpiresAt = Date.now() + 1000`);
  await env.exec(`api('/summary')`);
  assert.equal(authCallCount(env), 2);
  const refreshes = env.requests.filter((r) => r.url.endsWith('/token'));
  assert.equal(refreshes[0].opts.body.get('grant_type'), 'refresh_token');
  assert.equal(refreshes[0].opts.body.get('refresh_token'), 'refresh-1');
  assert.equal(refreshes[1].opts.body.get('refresh_token'), 'refresh-2');
  assert.equal(env.get('oidcRefreshToken'), 'refresh-3');
});

test('refresh failure: concurrent calls fail closed; no protected API is sent', async () => {
  const env = makeEnvironment(async (url) => {
    if (url.endsWith('/token')) {
      await new Promise((r) => setTimeout(r, 5));
      return {ok: false, status: 400, json: async () => ({error: 'invalid_grant'})};
    }
    return success({ok: true});
  });
  beginKeycloak(env);
  const result = await env.exec(`Promise.allSettled([api('/accounts'), api('/summary'), api('/auth/me')])`);
  assert.ok(result.every((r) => r.status === 'rejected'));
  assert.equal(authCallCount(env), 1);
  assert.equal(apiRequests(env).length, 0);
  assert.equal(env.get('token'), null);
  assert.equal(env.get('oidcRefreshToken'), null);
  assert.equal(env.get('authSource'), null);
});

test('logout during in-flight refresh: late success cannot restore tokens or make API call', async () => {
  let completeRefresh;
  const env = makeEnvironment(async (url) => {
    if (url.endsWith('/token')) {
      return new Promise((resolve) => {completeRefresh = resolve;});
    }
    return success({ok: true});
  });
  beginKeycloak(env);
  const pending = env.exec(`api('/accounts')`);
  // Attach a rejection handler before resolving the mocked network call.
  const settled = pending.then(() => 'resolved', () => 'rejected');
  assert.equal(typeof completeRefresh, 'function');
  env.exec(`signOutWithKeycloak()`);
  assert.equal(env.get('token'), null);
  assert.equal(env.get('oidcRefreshToken'), null);
  assert.equal(env.navigations.length, 1);
  assert.ok(new URL(env.navigations[0]).pathname.endsWith('/protocol/openid-connect/logout'));
  completeRefresh(success({access_token: 'late-access', refresh_token: 'late-refresh', expires_in: 300}));
  assert.equal(await settled, 'rejected');
  assert.equal(env.get('token'), null);
  assert.equal(env.get('oidcRefreshToken'), null);
  assert.equal(apiRequests(env).length, 0);
});

test('new login during old refresh: late refresh cannot overwrite new session', async () => {
  let completeRefresh;
  const env = makeEnvironment(async (url) => {
    if (url.endsWith('/token')) return new Promise((resolve) => {completeRefresh = resolve;});
    return success({ok: true});
  });
  beginKeycloak(env);
  const settled = env.exec(`api('/accounts')`).then(() => 'resolved', () => 'rejected');
  // Model the identity state change performed by the successful callback.
  env.exec(`
    oidcSessionGeneration += 1;
    oidcRefreshPromise = null;
    token = 'new-login-access';
    oidcRefreshToken = 'new-login-refresh';
    oidcAccessExpiresAt = Date.now() + 300000;
  `);
  completeRefresh(success({access_token: 'old-late-access', refresh_token: 'old-late-refresh', expires_in: 300}));
  assert.equal(await settled, 'rejected');
  assert.equal(env.get('token'), 'new-login-access');
  assert.equal(env.get('oidcRefreshToken'), 'new-login-refresh');
  assert.equal(apiRequests(env).length, 0);
});

test('local JWT requests never call Keycloak refresh; local logout does not navigate', async () => {
  const env = makeEnvironment(async () => success({ok: true}));
  env.exec(`token = 'local-jwt'; authSource = 'local';`);
  await env.exec(`api('/auth/me')`);
  assert.equal(authCallCount(env), 0);
  assert.equal(apiRequests(env)[0].opts.headers.Authorization, 'Bearer local-jwt');
  env.exec(`signOutWithKeycloak()`);
  assert.equal(env.get('token'), null);
  assert.equal(env.navigations.length, 0);
});

test('backend 401 logs out without repeating the protected request', async () => {
  const env = makeEnvironment(async () => ({ok: false, status: 401, json: async () => ({detail: 'Unauthorized'})}));
  env.exec(`token = 'local-jwt'; authSource = 'local';`);
  try { await env.exec(`api('/accounts')`); } catch (_) {}
  assert.equal(env.get('token'), null);
  assert.equal(apiRequests(env).length, 1);
});

test('script uses in-memory refresh credentials and includes session-generation guard', () => {
  assert.match(html, /let\s+oidcRefreshToken\s*=\s*null/);
  assert.match(html, /oidcSessionGeneration\s*!==\s*generation/);
  assert.match(html, /sessionStorage\.setItem\(OIDC_PENDING/);
  assert.doesNotMatch(html, /localStorage\.setItem\s*\(/);
  assert.doesNotMatch(html, /sessionStorage\.setItem\s*\(\s*['"](?:access_token|refresh_token)/);
});

test('valid access token is not refreshed prematurely', async () => {
  const env = makeEnvironment(async () => success({ok: true}));
  beginKeycloak(env);
  env.exec(`oidcAccessExpiresAt = Date.now() + 120000`);
  await env.exec(`api('/accounts')`);
  assert.equal(authCallCount(env), 0);
  assert.equal(apiRequests(env)[0].opts.headers.Authorization, 'Bearer access-1');
});

test('missing/invalid rotated refresh token fails closed', async () => {
  const env = makeEnvironment(async (url) => {
    if (url.endsWith('/token')) return success({access_token: 'access-2', expires_in: 300});
    return success({ok: true});
  });
  beginKeycloak(env);
  await assert.rejects(env.exec(`api('/accounts')`));
  assert.equal(env.get('token'), null);
  assert.equal(env.get('oidcRefreshToken'), null);
  assert.equal(apiRequests(env).length, 0);
});

test('SSO logout uses configured client and redirect, clears memory before navigation', () => {
  const env = makeEnvironment(async () => success({ok: true}));
  beginKeycloak(env);
  env.exec(`signOutWithKeycloak()`);
  assert.equal(env.get('token'), null);
  assert.equal(env.get('oidcIdToken'), null);
  const url = new URL(env.navigations[0]);
  assert.equal(url.searchParams.get('client_id'), 'moneta');
  assert.equal(url.searchParams.get('post_logout_redirect_uri'), 'http://127.0.0.1:8765/demo');
  assert.equal(url.searchParams.get('id_token_hint'), 'id-1');
  assert.ok(env.storageRemovals.includes('moneta.oidc.pkce.v1'));
});

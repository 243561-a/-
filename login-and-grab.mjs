// 受控登录窗口：打开一个独立配置的浏览器，用户扫码登录抖音后自动抓取 Cookie
//
// 为什么不用现成的 Chrome 配置：Chrome 136+ 禁止在默认配置目录上开远程调试，
// 且 Cookie 用 App-Bound 加密（v20），进程外解不开。
// 用一个全新的、非默认的配置目录，Playwright 就能正常工作。

import { chromium } from 'playwright-core';
import fs from 'fs';
import path from 'path';

const USE_EDGE = process.env.RL_BROWSER === 'edge';
const PROFILE = path.join(process.cwd(), USE_EDGE ? '.login-profile-edge' : '.login-profile');
const OUT = path.join(process.cwd(), 'cookie.txt');
const EXE = USE_EDGE
  ? 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
  : 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

console.log('使用浏览器: ' + (USE_EDGE ? 'Edge' : 'Chrome'));

const ctx = await chromium.launchPersistentContext(PROFILE, {
  executablePath: EXE,
  headless: false,
  viewport: { width: 1280, height: 860 },
  locale: 'zh-CN',
  args: ['--no-first-run', '--no-default-browser-check',
    '--disable-blink-features=AutomationControlled'],
});

const page = ctx.pages()[0] || await ctx.newPage();

// 换号场景：先把这个窗口里的登录态清掉，否则一打开就抓到旧账号、窗口秒关，
// 用户根本没机会扫码换成要开播的那个号。
if (process.env.RL_FRESH === '1') {
  try {
    await ctx.clearCookies();
    console.log('已清除本窗口的登录态，请扫码登录要开播的新号。');
  } catch (e) {
    console.log('清除登录态失败: ' + String(e).slice(0, 80));
  }
}

await page.goto('https://www.douyin.com/', { waitUntil: 'domcontentloaded', timeout: 60000 });

console.log('窗口已打开，请在窗口里扫码登录抖音。');
console.log('登录成功后我会自动抓取，不用你操作。');

let done = false;
const deadline = Date.now() + 300_000; // 5 分钟

while (Date.now() < deadline) {
  await new Promise(r => setTimeout(r, 2500));
  let cookies = [];
  try {
    cookies = await ctx.cookies('https://www.douyin.com');
  } catch { continue; }

  const hit = cookies.find(c => c.name === 'sessionid' && c.value && c.value.length > 10);
  if (!hit) continue;

  const keep = cookies.filter(c => /douyin\.com$/i.test(c.domain));
  const byName = new Map();
  for (const c of keep) byName.set(c.name, c);

  const order = ['sessionid', 'sessionid_ss', 'sid_tt', 'uid_tt', 'ttwid',
    'passport_csrf_token', 'msToken', 'odin_tt', 'sid_guard',
    'passport_csrf_token_default', 'tt_scid', 'UIFID_TEMP'];
  const parts = [], used = new Set();
  for (const n of order) {
    const c = byName.get(n);
    if (c) { parts.push(`${c.name}=${c.value}`); used.add(c.name); }
  }
  for (const c of keep) {
    if (used.has(c.name) || c.value.length > 600) continue;
    parts.push(`${c.name}=${c.value}`);
    used.add(c.name);
    if (parts.length >= 32) break;
  }

  fs.writeFileSync(OUT, parts.join('; '), 'utf8');
  try { fs.chmodSync(OUT, 0o600); } catch {}

  console.log(`登录成功，已抓到 sessionid（${hit.value.length} 字符）`);
  console.log(`已写入 ${OUT}，字段 ${parts.length} 个`);
  console.log('nickname:', await page.evaluate(() => {
    const el = document.querySelector('[data-e2e="user-info"] img');
    return el ? 'has-avatar' : 'unknown';
  }).catch(() => 'unknown'));
  done = true;
  break;
}

await ctx.close();

if (!done) {
  console.log('超时未检测到登录，未写入文件。');
  process.exitCode = 5;
}

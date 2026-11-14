// 用已登录的受控浏览器打开自己的抖音主页，把正在直播的直播间号（web_rid）抓回来
//
// 为什么必须走浏览器：douyin.com 会给纯 HTTP 请求返回一层 JS 反爬壳
// （_$jsvmprt），HTML 里没有任何数据，只有执行 JS 之后才会渲染出直播入口。

import { chromium } from 'playwright-core';
import fs from 'fs';
import path from 'path';

const T0 = Date.now();
const CWD = process.cwd();
const CANDIDATES = [
  ['.login-profile-edge', 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'],
  ['.login-profile-edge', 'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'],
  ['.login-profile', 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'],
  ['.login-profile', 'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe'],
];

let profile = '', exe = '';
for (const [dir, p] of CANDIDATES) {
  if (fs.existsSync(path.join(CWD, dir)) && fs.existsSync(p)) {
    profile = path.join(CWD, dir);
    exe = p;
    break;
  }
}

function emit(obj) {
  obj.ms = Date.now() - T0;
  console.log(JSON.stringify(obj));
}

if (!profile) {
  emit({ ok: false, msg: '找不到已登录的浏览器配置，先用「换号登录」登录一次' });
  process.exit(0);
}

async function open() {
  return await chromium.launchPersistentContext(profile, {
    executablePath: exe,
    headless: true,
    locale: 'zh-CN',
    args: ['--no-first-run', '--no-default-browser-check',
      '--disable-blink-features=AutomationControlled'],
  });
}

// 上一个窗口刚关时配置目录会被短暂占用，等 2 秒重试一次比让 Python 重启进程划算
let ctx = null, launchErr = '';
for (let i = 0; i < 2; i++) {
  try {
    ctx = await open();
    break;
  } catch (e) {
    launchErr = String(e).slice(0, 60);
    await new Promise(r => setTimeout(r, 2000));
  }
}
if (!ctx) {
  emit({ ok: false, msg: '打不开浏览器配置（可能窗口还开着）：' + launchErr });
  process.exit(0);
}

const page = ctx.pages()[0] || await ctx.newPage();

const grab = () => page.evaluate(() => {
  const links = Array.from(document.querySelectorAll('a[href*="live.douyin.com/"]'));
  for (const el of links) {
    const m = (el.href || '').match(/live\.douyin\.com\/(\d{6,})/);
    if (m) return m[1];
  }
  return '';
}).catch(() => '');

let rid = '', title = '';
try {
  // 反爬壳里有长跑脚本，domcontentloaded 可能迟迟不触发，只等响应到达
  await page.goto('https://www.douyin.com/user/self',
    { waitUntil: 'commit', timeout: 20000 });
  // 再等直播入口渲染出来（这是元素级的等待，不受前面卡住的脚本影响）
  await page.waitForSelector('a[href*="live.douyin.com/"]', { timeout: 12000 })
    .catch(() => {});
  rid = await grab();
  for (let i = 0; !rid && i < 6; i++) {
    await page.waitForTimeout(700);
    rid = await grab();
  }
  if (!rid) title = await page.title().catch(() => '');
} catch (e) {
  emit({ ok: false, msg: '打开抖音主页失败：' + String(e).slice(0, 60) });
  ctx.close().catch(() => {});
  process.exit(0);
}

emit({ ok: !!rid, web_rid: rid, title: title || '' });
// 关闭浏览器要几秒，不等它，抓完就退，省 2~3 秒
ctx.close().catch(() => {});
setTimeout(() => process.exit(0), 300);

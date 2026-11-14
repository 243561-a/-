#!/usr/bin/env python
# coding: utf-8
"""
直播控制台（遥控器）：程序跑在机器人上，你用电脑或手机的浏览器来指挥。

为什么不是"把程序搬到电脑上"：
- 实时链路（采集→规则→播报）必须常驻、必须在机器人旁边，断一分钟观众就看到了
- 你想临时插话、屏蔽话题，这是"控制面"，本来就该跟"数据面"分开
- 控制面断开（关电脑、手机锁屏）不影响机器人继续工作

接口（全部要带 token）：
    GET  /              控制台页面
    GET  /api/state     状态快照
    POST /api/say       {"text": "...", "priority": 9, "interrupt": true}
    POST /api/pause     {"minutes": 5}
    POST /api/resume    {}
    POST /api/ban       {"word": "多少钱"}
    POST /api/unban     {"word": "多少钱"}
"""

import http.server
import json
import os
import secrets
import threading
import time
from urllib.parse import urlparse, parse_qs

PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>远仔 · 直播控制台</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--fg:#1b1d21;--mut:#6b7280;--line:#e3e5e9;--acc:#2f6feb}
@media(prefers-color-scheme:dark){:root{--bg:#13151a;--card:#1b1e24;--fg:#e8eaed;--mut:#98a0ac;--line:#2a2f37;--acc:#5b8cf7}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:640px;margin:0 auto;padding:16px 14px 48px}
h1{font-size:16px;margin:0 0 12px;font-weight:500}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
.card h2{font-size:12px;margin:0 0 10px;font-weight:500;color:var(--mut)}
.stat{display:flex;gap:20px;flex-wrap:wrap}
.stat div{font-size:12px;color:var(--mut)}
.stat b{display:block;font-size:17px;font-weight:500;color:var(--fg)}
textarea,input{width:100%;border:1px solid var(--line);border-radius:8px;padding:9px 10px;font:inherit;background:transparent;color:var(--fg)}
textarea{height:68px;resize:vertical}
button{border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:8px;padding:8px 12px;font:inherit;cursor:pointer}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
button:active{opacity:.7}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.chip{display:inline-flex;align-items:center;gap:6px;background:var(--bg);border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:12px;cursor:pointer}
.chip i{font-style:normal;color:var(--mut);cursor:pointer}
.hint{font-size:12px;color:var(--mut);margin-top:8px}
#toast{font-size:12px;color:var(--acc);margin-top:8px;min-height:18px}
</style></head><body>
<div class="wrap">
<h1>远仔 · 直播控制台</h1>

<div class="card">
  <h2>实时状态</h2>
  <div class="stat">
    <div><b id="s_age">—</b>最近消息</div>
    <div><b id="s_gift">—</b>礼物</div>
    <div><b id="s_said">—</b>已开口</div>
    <div><b id="s_mode">—</b>状态</div>
  </div>
  <div class="hint" id="s_last">—</div>
</div>

<div class="card">
  <h2>临时播报 · 我有个想法</h2>
  <textarea id="say" placeholder="打一句话，机器人立刻说出来"></textarea>
  <div class="row" style="margin-top:8px">
    <button class="primary" onclick="sendSay(true)">立刻说（打断）</button>
    <button onclick="sendSay(false)">排队说</button>
  </div>
  <div class="chips" id="presets"></div>
  <div id="toast"></div>
</div>

<div class="card">
  <h2>不回答清单</h2>
  <div class="row">
    <input id="ban" placeholder="加一个词，比如「多少钱」" style="flex:1;min-width:160px">
    <button onclick="addBan()">添加</button>
  </div>
  <div class="chips" id="bans"></div>
  <div class="hint">命中就不答、不道谢，改完立即生效。</div>
</div>

<div class="card">
  <h2>暂停自动应答</h2>
  <div class="row">
    <button onclick="pause(5)">5 分钟</button>
    <button onclick="pause(15)">15 分钟</button>
    <button onclick="pause(30)">30 分钟</button>
    <button class="primary" onclick="resume()">立即恢复</button>
  </div>
  <div class="hint">暂停期间不自动谢礼物，临时播报照样能用。</div>
</div>
</div>

<script>
const PRESETS = ['欢迎新来的朋友，我是远仔', '稍等一下，马上就好', '这波礼物我记下了，谢谢'];
const $ = id => document.getElementById(id);

async function api(path, body){
  const r = await fetch(path, {
    method: body ? 'POST' : 'GET',
    headers: {'Content-Type':'application/json'},
    body: body ? JSON.stringify(body) : undefined
  });
  return r.json();
}

function toast(msg){ $('toast').textContent = msg; }

async function refresh(){
  try{
    const s = await api('/api/state');
    $('s_age').textContent = s.idle_s === null ? '—' : s.idle_s + 's';
    $('s_gift').textContent = s.gifts;
    $('s_said').textContent = s.spoken;
    $('s_mode').textContent = s.paused ? '已暂停' : '运行中';
    $('s_last').textContent = s.last_text || '还没有说过话';
    $('bans').innerHTML = '';
    (s.bans || []).forEach(w => {
      const c = document.createElement('span');
      c.className = 'chip';
      const label = document.createElement('span');
      label.textContent = w;
      const x = document.createElement('i');
      x.textContent = '×';
      x.onclick = async () => { await api('/api/unban', {word: w}); refresh(); };
      c.appendChild(label);
      c.appendChild(x);
      $('bans').appendChild(c);
    });
  }catch(e){ $('s_mode').textContent = '连不上'; }
}

async function sendSay(interrupt){
  const text = $('say').value.trim();
  if(!text) return toast('先说点什么');
  await api('/api/say', {text, priority: interrupt ? 9 : 6, interrupt: !!interrupt});
  $('say').value = '';
  toast('已发送');
  refresh();
}

async function addBan(){
  const w = $('ban').value.trim();
  if(!w) return;
  await api('/api/ban', {word: w});
  $('ban').value = '';
  refresh();
}

async function pause(m){ await api('/api/pause', {minutes: m}); refresh(); }
async function resume(){ await api('/api/resume', {}); refresh(); }

PRESETS.forEach(t => {
  const c = document.createElement('span');
  c.className = 'chip';
  c.textContent = t;
  c.onclick = () => { $('say').value = t; };
  $('presets').appendChild(c);
});

refresh();
setInterval(refresh, 3000);
</script>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        pass

    def _token_ok(self, qs=None):
        token = self.server.token
        if not token:
            return True
        if qs and qs.get('t', [None])[0] == token:
            return True
        return token in (self.headers.get('Cookie') or '')

    def _json(self, obj, code=200, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        if cookie:
            self.send_header('Set-Cookie', f'admin_token={cookie}; Path=/; SameSite=Lax')
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path in ('/', '/index.html'):
            if not self._token_ok(qs):
                return self._json({'error': 'token 不对'}, 403)
            body = PAGE.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            if qs.get('t'):
                self.send_header('Set-Cookie', f'admin_token={qs["t"][0]}; Path=/; SameSite=Lax')
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == '/api/state':
            if not self._token_ok(qs):
                return self._json({'error': 'token 不对'}, 403)
            return self._json(self.server.ctx.snapshot())
        if u.path == '/api/experience':
            if not self._token_ok(qs):
                return self._json({'error': 'token 不对'}, 403)
            return self._json(self.server.ctx.experience_snapshot())
        self._json({'error': 'not found'}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._token_ok(parse_qs(u.query)):
            return self._json({'error': 'token 不对'}, 403)
        data = self._body()
        ctx = self.server.ctx

        if u.path == '/api/say':
            text = (data.get('text') or '').strip()[:200]
            if not text:
                return self._json({'ok': False, 'error': '空文本'})
            pri = int(data.get('priority') or 9)
            interrupt = bool(data.get('interrupt', True))
            ctx.say(text, pri, interrupt)
            return self._json({'ok': True})

        if u.path == '/api/reply-danmaku':
            name = (data.get('name') or '').strip()[:20]
            text = (data.get('text') or '').strip()[:300]
            ok, msg = ctx.answer_danmaku(name, text, data.get('uid') or '')
            return self._json({'ok': ok, 'msg': msg})

        # ---------- 知识库 / 高频问题 ----------
        if u.path == '/api/experience':
            return self._json(ctx.experience_snapshot())

        if u.path == '/api/experience/accept':
            cid = (data.get('id') or '').strip()
            if not cid:
                return self._json({'ok': False, 'msg': '缺少问题 id'})
            ok, msg, extra = ctx.accept_suggestion(
                cid, data.get('text'), data.get('keywords'))
            out = {'ok': ok, 'msg': msg}
            out.update(extra or {})
            return self._json(out)

        if u.path == '/api/experience/dismiss':
            eg = ctx.reply_engine
            if eg is None or getattr(eg, 'experience', None) is None:
                return self._json({'ok': False, 'msg': '问答未启用'})
            ok = eg.experience.dismiss((data.get('id') or '').strip())
            return self._json({'ok': ok, 'msg': '' if ok else '没找到这条问题'})

        if u.path == '/api/experience/regenerate':
            eg = ctx.reply_engine
            if eg is None or getattr(eg, 'experience', None) is None:
                return self._json({'ok': False, 'msg': '问答未启用'})
            ok = eg.experience.regenerate((data.get('id') or '').strip())
            return self._json({'ok': ok,
                               'msg': '已排队，稍后刷新看新建议' if ok else '没找到这条问题'})

        if u.path == '/api/experience/settings':
            eg = ctx.reply_engine
            if eg is None or getattr(eg, 'experience', None) is None:
                return self._json({'ok': False, 'msg': '问答未启用'})
            try:
                got = eg.experience.update_settings(data or {})
                return self._json({'ok': True, 'settings': got})
            except Exception as e:
                return self._json({'ok': False, 'msg': str(e)})

        if u.path == '/api/pause':
            minutes = float(data.get('minutes') or 5)
            ctx.paused_until = time.time() + minutes * 60
            return self._json({'ok': True, 'until': ctx.paused_until})

        if u.path == '/api/resume':
            ctx.paused_until = 0
            return self._json({'ok': True})

        if u.path == '/api/ban':
            ok = ctx.bans.add((data.get('word') or '')[:40])
            return self._json({'ok': ok})

        if u.path == '/api/unban':
            ok = ctx.bans.remove((data.get('word') or '')[:40])
            return self._json({'ok': ok})

        self._json({'error': 'not found'}, 404)


class AdminContext:
    """控制台与运行中的管线之间的唯一接口，避免两边互相引用。"""

    def __init__(self, worker=None, bans=None, room=''):
        self.worker = worker
        self.bans = bans
        self.room = room
        self.paused_until = 0
        self.started = time.time()
        self.gifts = 0
        self.spoken = 0
        self.last_event_ts = 0
        self.last_text = ''
        self.blocked = 0
        # 由 pipeline 注入：控制台点一条弹幕时，用它生成回答并插进串行队列
        self.reply_engine = None
        self.reply_worker = None

    def answer_danmaku(self, name, text, uid=''):
        """人工点名让机器人回一条弹幕。

        和自动回复的区别：不检查唤醒词、不占用个人冷却（是主播手动点的），
        但仍然走 ReplyWorker 的串行队列，不会和礼物话术抢麦。
        """
        eg, wk = self.reply_engine, self.reply_worker
        if eg is None or wk is None:
            return False, '弹幕回复未启用'
        has, q = eg.parse(text or '')
        if not has:                       # 手动触发时，整条都当问题
            q = (text or '').strip()
        q = q[:eg.max_q]
        if not q:
            return False, '这条弹幕没有内容'
        item = {
            'uid': uid or ('manual:' + (name or '')),
            'name': (name or '朋友')[:10],
            'question': q,
            'text': '', 'source': '', 'ts': int(time.time() * 1000),
        }
        ok = wk.enqueue(item)
        return (True, '已排入队列') if ok else (False, '队列已满，稍等再点')

    def paused(self):
        return time.time() < self.paused_until

    def note_event(self):
        self.last_event_ts = time.time()

    def say(self, text, priority=9, interrupt=True):
        """人工插话：永远允许，即使处于暂停状态。"""
        if self.worker is None:
            return
        self.worker.submit({
            'text': text, 'priority': priority, 'interrupt': interrupt,
            'manual': True,
        })

    def note_spoken(self, text):
        self.spoken += 1
        self.last_text = text

    # ---------- 知识库 / 高频问题 ----------
    def experience_snapshot(self):
        """经验库读接口：只有管线进程能给出实时计数。"""
        eg = self.reply_engine
        if eg is None or getattr(eg, 'experience', None) is None:
            return {'ok': False, 'msg': '问答未启用', 'clusters': []}
        exp = eg.experience
        return {'ok': True, 'stats': exp.stats(), 'settings': exp.settings(),
                'clusters': exp.all_clusters()}

    def accept_suggestion(self, cid, text=None, keywords=None):
        eg = self.reply_engine
        if eg is None:
            return False, '问答未启用', {}
        return eg.accept_suggestion(cid, text, keywords)

    def snapshot(self):
        idle = None
        if self.last_event_ts:
            idle = int(time.time() - self.last_event_ts)
        return {
            'room': self.room,
            'uptime_s': int(time.time() - self.started),
            'idle_s': idle,
            'gifts': self.gifts,
            'spoken': self.spoken,
            'blocked': self.blocked,
            'paused': self.paused(),
            'paused_left_s': max(0, int(self.paused_until - time.time())),
            'last_text': self.last_text,
            'bans': list(self.bans.words) if self.bans else [],
        }


class AdminServer(threading.Thread):
    # 只监听本机：控制台（gui.py）走 127.0.0.1 调它，没有任何远程访问的需求。
    # 绑 0.0.0.0 会让管理接口出现在局域网扫描结果里，白送一个入口。
    def __init__(self, ctx, port=8770, host='127.0.0.1', token=None):
        super().__init__(daemon=True)
        self.ctx = ctx
        self.port = port
        self.host = host
        self.token = token or ''
        self.httpd = None

    def run(self):
        try:
            self.httpd = http.server.ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as e:
            print(f'  [控制台] 端口 {self.port} 没起来：{e}')
            return
        self.httpd.ctx = self.ctx
        self.httpd.token = self.token
        self.httpd.serve_forever(poll_interval=0.5)

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()


def load_or_create_token(path):
    """首次启动生成随机口令，存到 600 权限的文件里，别写进代码。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            t = f.read().strip()
            if t:
                return t
    except OSError:
        pass
    t = secrets.token_urlsafe(12)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(t)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return t

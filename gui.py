#!/usr/bin/env python
# coding: utf-8
"""
RobotLive 控制台（GUI）

两件事：
  1. 检测哪个直播间 —— 填房间号，启停采集，看实时事件
  2. 设定礼物与对应反馈 —— 改话术、加“指定礼物专属话术”，保存即生效

用法：
    ./.venv/Scripts/python.exe gui.py           # 默认 http://127.0.0.1:8780
    ./.venv/Scripts/python.exe gui.py --port 8888

只监听 127.0.0.1，不对外暴露。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 配置文件一律原子写：pipeline 每 200ms 热加载一次，不能让它读到半截 JSON
from jsonio import write_json

HERE = os.path.dirname(os.path.abspath(__file__))
GIFTS = os.path.join(HERE, 'gifts.json')
REPLIES = os.path.join(HERE, 'replies.json')
INTERACTIONS = os.path.join(HERE, 'interactions.json')
KNOWLEDGE = os.path.join(HERE, 'knowledge.md')
EXPERIENCE = os.path.join(HERE, 'experience.json')
STATE = os.path.join(HERE, 'gui-state.json')

LOCK = threading.Lock()
LOG = deque(maxlen=500)
COUNTS = {'gift': 0, 'speak': 0, 'chat': 0, 'enter': 0, 'reply': 0}
# 最近采集到的弹幕，供页面点选"让机器人回这条"
CHATS = deque(maxlen=60)
# pipeline 内部管理端口（控制台用它点名回复弹幕）
ADMIN_PORT = 8771
BOOT = time.strftime('%m-%d %H:%M:%S')


class Runner:
    def __init__(self):
        self.proc = None
        self.room = ''
        self.started = 0

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, room, source='go'):
        if self.running():
            return False, '已经在运行'
        with LOCK:
            LOG.clear()
            CHATS.clear()
            for k in COUNTS:
                COUNTS[k] = 0
        cmd = [sys.executable, '-u', os.path.join(HERE, 'pipeline.py'),
               room, '--source', source, '--admin-port', str(ADMIN_PORT)]
        self.proc = subprocess.Popen(
            cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', bufsize=1,
            env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.room = room
        self.started = int(time.time())
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        return True, f'已启动，直播间 {room}'

    def _pump(self, p):
        for line in p.stdout:
            line = line.rstrip()
            if not line:
                continue
            with LOCK:
                LOG.append(line)
                # 只认真实事件行（以 [时间] 开头），避免把提示文案当成事件；
                # 播报行前面有缩进，所以先 strip 再判断
                core = line.strip()
                if '播报OK' in core:
                    COUNTS['speak'] += 1
                    continue
                if '[弹幕回复] ok:' in core:
                    COUNTS['reply'] += 1
                    continue
                if not core.startswith('['):
                    continue
                if '收到礼物' in core:
                    COUNTS['gift'] += 1
                elif '弹幕 ' in core:
                    COUNTS['chat'] += 1
                    m = re.match(r'^\[(\d{2}:\d{2}:\d{2})\]\s*弹幕\s+(.*?)：(.*)$', core)
                    if m:
                        tx = m.group(3).strip()
                        if '  [' in tx:        # 日志并发时偶发两行粘连，切掉尾随部分
                            tx = tx.split('  [', 1)[0].strip()
                        CHATS.append({'id': time.time_ns(),
                                      't': m.group(1), 'name': m.group(2).strip(),
                                      'text': tx})
                elif '进场 ' in core:
                    COUNTS['enter'] += 1

    def stop(self):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
            with LOCK:
                LOG.append('[gui] 已停止')
            return True
        return False


RUNNER = Runner()

# ---------------------------------------------------------------- 采集服务自启
from backend import GO_BIN, GO_CFG, go_alive as _go_alive, ensure_backend  # noqa: E402
GO_HEALTH = 'http://127.0.0.1:1088/health'


def load_state():
    try:
        with open(STATE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'room': '705704016990'}


def save_state(st):
    try:
        write_json(STATE, st)
    except Exception:
        pass


def admin_call(path, payload, timeout=6):
    """把指令转给 pipeline 的管理端口（它才是持有播报队列的进程）。"""
    import urllib.request
    try:
        with open(os.path.join(HERE, 'admin-token.txt'), encoding='utf-8') as f:
            token = f.read().strip()
    except OSError:
        return {'ok': False, 'msg': '还没有管理令牌，先启动一次采集'}
    url = 'http://127.0.0.1:%d%s?t=%s' % (ADMIN_PORT, path, token)
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=body,
                                 headers={'Content-Type': 'application/json'})
    # 本机回环必须绕开系统代理，否则会被代理软件拦走
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return {'ok': False, 'msg': '管线没响应（%s），确认采集在运行' % type(e).__name__}


def load_gifts():
    with open(GIFTS, encoding='utf-8') as f:
        return json.load(f)


def load_replies():
    with open(REPLIES, encoding='utf-8') as f:
        return json.load(f)


def load_interactions():
    """点赞/关注话术。文件还没建就用内置默认（pipeline 那边也是同一套默认）"""
    try:
        with open(INTERACTIONS, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        from interactions import DEFAULT
        return {'version': 1,
                'like': dict(DEFAULT['like']),
                'follow': dict(DEFAULT['follow']),
                'max_name_chars': DEFAULT['max_name_chars'],
                'forbidden_words': list(DEFAULT['forbidden_words'])}


def load_knowledge():
    """知识库：knowledge.md 是唯一来源，页面只做浏览，不提供在线编辑。"""
    from knowledge import KnowledgeBase
    kb = KnowledgeBase(KNOWLEDGE)
    return {'version': 1, 'path': 'knowledge.md', 'readonly': True,
            'entries': kb.all()}


def local_experience():
    """采集没在跑时直接读本地经验文件（只读，不起后台线程）。"""
    from experience import ExperienceStore
    exp = ExperienceStore(EXPERIENCE)
    return {'ok': True, 'offline': True, 'stats': exp.stats(),
            'settings': exp.settings(), 'clusters': exp.all_clusters()}


def experience_call(path, payload):
    """优先交给管线（唯一写者）；连不上时才退回本地写，避免双写打架。"""
    r = admin_call(path, payload)
    if r.get('ok') or ('ok' in r and '管线没响应' not in (r.get('msg') or '')):
        return r
    from experience import ExperienceStore
    exp = ExperienceStore(EXPERIENCE)
    cid = (payload.get('id') or '').strip()
    if path == '/api/experience/dismiss':
        ok = exp.dismiss(cid)
        return {'ok': ok, 'offline': True, 'msg': '' if ok else '没找到这条问题'}
    if path == '/api/experience/regenerate':
        ok = exp.regenerate(cid)
        return {'ok': ok, 'offline': True,
                'msg': '采集未运行，已标记；下次启动后会生成建议' if ok else '没找到这条问题'}
    if path == '/api/experience/settings':
        try:
            return {'ok': True, 'offline': True,
                    'settings': exp.update_settings(payload or {})}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}
    if path == '/api/experience/accept':
        entry = exp.accept(cid, payload.get('text'), payload.get('keywords'))
        if not entry:
            return {'ok': False, 'offline': True, 'msg': '这条还没有可采纳的回答'}
        from knowledge import KnowledgeBase
        KnowledgeBase(KNOWLEDGE).upsert(entry)
        return {'ok': True, 'offline': True, 'entry': entry,
                'msg': '已采纳（采集未运行，已写入知识库，下次启动生效）'}
    return r


def _cookie_raw():
    """从 bin/config.yaml 里取出 cookie.douyin 那段"""
    try:
        with open(GO_CFG, encoding='utf-8') as f:
            txt = f.read()
    except OSError:
        return None
    m = re.search(r'(?m)^\s*douyin:\s*"(.*)"\s*$', txt)
    if not m:
        return None
    return m.group(1).replace('\\"', '"').replace('\\\\', '\\')


def cookie_status():
    """只报概况，不回显具体值"""
    raw = _cookie_raw()
    if raw is None:
        return {'ok': False, 'msg': '读不到 bin/config.yaml 里的 douyin 配置'}
    kv = {}
    for seg in raw.split(';'):
        if '=' in seg:
            k, v = seg.split('=', 1)
            kv[k.strip()] = v.strip()
    try:
        ts = time.strftime('%m-%d %H:%M', time.localtime(os.path.getmtime(GO_CFG)))
    except OSError:
        ts = '-'
    return {'ok': True, 'fields': len(kv),
            'has_session': bool(kv.get('sessionid')),
            'updated': ts,
            'keys': sorted(kv)[:10]}


def save_cookie(raw_text):
    """把粘贴进来的 Cookie 写进 bin/config.yaml（支持纯串 / Cookie: / curl）"""
    from set_cookie import extract_cookie, parse
    raw = extract_cookie(raw_text or '')
    if not raw:
        return False, '没从这段内容里认出 Cookie（检查是不是复制到了空请求头）'
    kv = parse(raw)
    if not kv:
        return False, '解析不出键值对，这不像 Cookie'
    if not kv.get('sessionid') and not kv.get('sessionid_ss'):
        return False, '缺 sessionid，这多半不是登录态（只解析出 %d 个字段）' % len(kv)
    try:
        with open(GO_CFG, encoding='utf-8') as f:
            txt = f.read()
    except OSError as e:
        return False, '读不到 bin/config.yaml：%s' % e
    esc = raw.replace('\\', '\\\\').replace('"', '\\"')
    new, n = re.subn(r'(?m)^(\s*)douyin:\s*".*"\s*$',
                     lambda m: m.group(1) + 'douyin: "' + esc + '"', txt)
    if n == 0:
        return False, 'config.yaml 里找不到 douyin: 那一行（文件结构被改过？）'
    try:
        with open(GO_CFG, 'w', encoding='utf-8', newline='') as f:
            f.write(new)
    except OSError as e:
        return False, '写 config.yaml 失败：%s' % e
    return True, '已更新 %d 个字段（含 sessionid）' % len(kv)


def restart_backend():
    """Cookie 只在启动时读取，必须重启采集服务才生效"""
    try:
        subprocess.run(['taskkill', '/IM', 'douyinLive.exe', '/F'],
                       capture_output=True, timeout=15)
    except Exception:
        pass
    time.sleep(1.0)
    return ensure_backend()


# ---------- 一键获取 Cookie：受控浏览器窗口，扫码即抓 ----------
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

_BROWSERS = [
    ('edge', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'),
    ('edge', r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'),
    ('chrome', r'C:\Program Files\Google\Chrome\Application\chrome.exe'),
    ('chrome', r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'),
]

_AUTO = {'running': False, 'ok': None, 'step': '', 'msg': '', 'ts': 0}
_AUTO_LOCK = threading.Lock()


def _auto_set(**kw):
    with _AUTO_LOCK:
        _AUTO.update(kw)
        _AUTO['ts'] = time.time()


def auto_state():
    with _AUTO_LOCK:
        return dict(_AUTO)


def node_ready():
    """一键获取依赖：node + playwright-core + 本机 Edge/Chrome"""
    if not shutil.which('node'):
        return False, '这台电脑没装 Node.js，只能用下面的手动粘贴'
    if not os.path.isdir(os.path.join(HERE, 'node_modules', 'playwright-core')):
        return False, '缺少 playwright-core（项目目录执行 npm i playwright-core 后重启控制台）'
    for _, exe in _BROWSERS:
        if os.path.exists(exe):
            return True, ''
    return False, '没找到 Edge 或 Chrome'


def _pick_browser():
    for name, exe in _BROWSERS:
        if os.path.exists(exe):
            return name
    return ''


def auto_login_start(fresh=False):
    """开一个独立配置的浏览器窗口，用户扫码后自动抓 Cookie 并写回配置

    fresh=True 用于换号：先清掉窗口里的登录态，强制重新扫码，
    否则一打开就抓到旧账号、窗口秒关，用户来不及换号。
    """
    ok, why = node_ready()
    if not ok:
        return False, why
    with _AUTO_LOCK:
        if _AUTO['running']:
            return False, '窗口已经开着，去浏览器里扫码就行'
        _AUTO.update({'running': True, 'ok': None, 'step': 'launch',
                      'msg': '正在打开浏览器…', 'ts': time.time(),
                      'web_rid': ''})
    threading.Thread(target=_auto_login_worker, args=(_pick_browser(), bool(fresh)),
                     daemon=True).start()
    return True, '已打开浏览器窗口'


def _auto_login_worker(browser, fresh=False):
    out = os.path.join(HERE, 'cookie.txt')
    logp = os.path.join(HERE, 'runs', 'auto-login.log')
    try:
        before = os.path.getmtime(out)
    except OSError:
        before = 0
    env = dict(os.environ)
    env['RL_BROWSER'] = browser
    env['RL_FRESH'] = '1' if fresh else '0'
    try:
        os.makedirs(os.path.dirname(logp), exist_ok=True)
        logf = open(logp, 'w', encoding='utf-8', errors='replace')
    except OSError:
        logf = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(['node', 'login-and-grab.mjs'], cwd=HERE,
                                env=env, stdout=logf, stderr=subprocess.STDOUT)
    except Exception as e:
        try:
            logf.close()
        except Exception:
            pass
        _auto_set(running=False, ok=False, step='error',
                  msg='启动浏览器失败：%s' % e)
        return

    _auto_set(step='wait',
              msg='窗口已打开，请扫码登录要开播的那个号' if fresh
                  else '窗口已打开，请确认右上角是要开播的号')
    got, deadline = False, time.time() + 300
    while time.time() < deadline:
        time.sleep(1.2)
        try:
            now = os.path.getmtime(out)
        except OSError:
            now = before
        if now > before:
            time.sleep(1.2)          # 等脚本把文件写完
            got = True
            break
        if proc.poll() is not None:
            break

    try:
        logf.close()
    except Exception:
        pass

    if not got:
        hint = ''
        try:
            tail = open(logp, encoding='utf-8', errors='replace').read().strip()
            if tail:
                hint = '｜' + tail.splitlines()[-1][:120]
        except OSError:
            pass
        _auto_set(running=False, ok=False, step='timeout',
                  msg='没抓到 Cookie（窗口关了或超时 5 分钟），可以再点一次' + hint)
        return

    try:
        raw = open(out, encoding='utf-8').read().strip()
    except OSError as e:
        _auto_set(running=False, ok=False, step='error', msg='读 cookie.txt 失败：%s' % e)
        return

    _auto_set(step='save', msg='已抓到登录态，正在写入配置…')
    ok, msg = save_cookie(raw)
    if not ok:
        _auto_set(running=False, ok=False, step='error', msg=msg)
        return

    try:
        _WHO['data'] = _who_fetch()
    except Exception:
        pass
    who = (_WHO['data'] or {})
    tail = ('，当前账号 %s' % who.get('name')) if who.get('ok') else ''

    # 顺手把直播间号认出来：换号之后房间号也变了，不能拿旧的接着连
    _auto_set(step='save', msg='配置已写入，正在识别你的直播间号…')
    rid = ''
    try:
        d = detect_room()
        if d.get('ok'):
            rid = d.get('web_rid') or ''
    except Exception:
        pass

    was_running = RUNNER.running()
    old_room = RUNNER.room or (load_state().get('room') or '')
    target = rid or old_room
    if was_running:
        RUNNER.stop()
        time.sleep(0.6)
    _auto_set(step='save', msg='配置已写入，正在重启采集…')
    okb, msgb = restart_backend()
    resumed = False
    if was_running and okb and target:
        resumed, _ = RUNNER.start(target)

    note = ''
    if rid and old_room and rid != old_room:
        note = '；直播间号从 %s 换成了 %s' % (old_room, rid)
    elif rid:
        note = '；直播间号 %s' % rid
    _auto_set(running=False, ok=bool(okb), step='done', web_rid=rid,
              msg=msg + tail + note + ('；采集已重新接上' if resumed else ''))


def _http_web_rid(ck):
    """纯 HTTP 抢一次：抖音没启反爬壳时页面里就带 owner.web_rid，最快"""
    import urllib.parse
    import urllib.request
    req = urllib.request.Request(
        'https://www.douyin.com/user/self',
        headers={'Cookie': ck, 'User-Agent': UA,
                 'Referer': 'https://www.douyin.com/'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as r:
            html = r.read().decode('utf-8', 'replace')
    except Exception:
        return ''
    # 同一个 id 有三种写法：原样、"\"转义"、URL 编码，统一还原后再找
    text = urllib.parse.unquote(html).replace('\\"', '"')
    m = re.search(r'"owner"\s*:\s*\{\s*"web_rid"\s*:\s*"(\d{6,})"', text)
    if not m:
        m = re.search(r'live\.douyin\.com/(\d{6,})\?[^"]{0,80}'
                      r'enter_from_merge=personal_homepage', text)
    return m.group(1) if m else ''


def _browser_web_rid():
    """兜底：douyin.com 会返回 JS 反爬壳，用已登录的浏览器渲染后再抓"""
    node = shutil.which('node')
    if not node:
        return '', '这台电脑没装 Node.js，识别不了直播间号，手动填吧'
    try:
        r = subprocess.run([node, 'detect-room.mjs'], cwd=HERE,
                           capture_output=True, timeout=70,
                           text=True, encoding='utf-8', errors='replace')
    except subprocess.TimeoutExpired:
        return '', '浏览器超时'
    except Exception as e:
        return '', '调用浏览器失败：%s' % type(e).__name__
    out = (r.stdout or '').strip().splitlines()
    if not out:
        tail = (r.stderr or '').strip().splitlines()
        return '', (tail[-1][:80] if tail else '浏览器没有输出')
    try:
        d = json.loads(out[-1])
    except Exception:
        return '', out[-1][:80]
    if d.get('ok'):
        return d.get('web_rid') or '', ''
    return '', d.get('msg') or '浏览器没找到直播间入口'


def detect_room():
    """认当前 Cookie 是哪个号、正在播的直播间号是多少"""
    ck = _cookie_raw() or ''
    if not ck:
        return {'ok': False, 'msg': '还没配置 Cookie，先用下面的按钮获取'}
    if 'sessionid=' not in ck:
        return {'ok': False, 'msg': 'Cookie 里没有 sessionid，识别不到是哪个账号'}

    who = (_WHO['data'] or {})
    nick = who.get('name') if who.get('ok') else ''

    rid = _http_web_rid(ck)
    if rid:
        return {'ok': True, 'web_rid': rid, 'nick': nick, 'via': 'http'}

    t0 = time.time()
    rid, why = _browser_web_rid()
    print('  [识别直播间号] 浏览器兜底 %.1fs → %s' % (time.time() - t0, rid or why),
          flush=True)
    if rid:
        return {'ok': True, 'web_rid': rid, 'nick': nick, 'via': 'browser'}
    # 环境类问题（缺 Node、没有登录过的浏览器配置）直接说清楚，别怪到"没开播"头上
    if why and ('Node' in why or '浏览器配置' in why or why.startswith('浏览器')
                or why.startswith('调用浏览器') or why.startswith('打不开')):
        return {'ok': False, 'nick': nick, 'msg': why}
    return {'ok': False, 'nick': nick,
            'msg': '没找到直播间号 —— 主页只在开播时才挂直播入口，先开播再点这个按钮'
                   + ('（%s）' % why if why else '')}


def clean_name(raw, max_chars=10):
    name = re.sub(r'[^\w\u4e00-\u9fff]', '', (raw or '').strip()) or '这位朋友'
    return name[:max_chars]


def preview(body):
    """按当前话术表渲染一句样例，让用户保存前先听见效果"""
    cfg = body.get('cfg') or load_gifts()
    gift = body.get('gift') or '小心心'
    diamond = int(body.get('diamond') or 1)
    user = clean_name(body.get('user') or '小明', int(cfg.get('max_name_chars', 10)))
    count = int(body.get('count') or 1)

    tiers = sorted(cfg.get('tiers', []), key=lambda t: t.get('min_diamond', 0))
    tier = next((t for t in tiers
                 if t['min_diamond'] <= diamond <= t['max_diamond']), None)
    ov = (cfg.get('gift_overrides') or {}).get(gift)

    import random
    ov_tpls = [p for p in ((ov or {}).get('templates') or []) if (p or '').strip()]
    if ov_tpls:
        pool = ov_tpls
        label = ov.get('label') or '指定礼物'
        priority = ov.get('priority') or (tier or {}).get('priority', 8)
    else:
        if count > 1 and cfg.get('combo_templates'):
            pool = cfg['combo_templates']
        else:
            pool = (tier or {}).get('templates') or [cfg.get('fallback', '谢谢{user}的{gift}！')]
        label = (tier or {}).get('name', '未匹配')
        priority = (tier or {}).get('priority', 8)

    text = _pick_variant(pool, gift if ov_tpls else label).format(
        user=user, gift=gift, count=count)
    return {'text': text, 'tier': label, 'priority': priority,
            'hit_override': bool(ov_tpls)}


_PREVIEW_LAST = {}


def _pick_variant(pool, key):
    """随机抽一句，但避开上一次抽到的那句 —— 连点试听才不会老念叨同一句。"""
    import random as _r
    pool = [p for p in pool if (p or '').strip()] or ['']
    cand = pool
    prev = _PREVIEW_LAST.get(key)
    if len(pool) > 1 and prev in pool:
        cand = [p for p in pool if p != prev] or pool
    tpl = _r.choice(cand)
    _PREVIEW_LAST[key] = tpl
    return tpl


_WHO = {'data': None}


def _who_fetch():
    """用当前 cookie 问抖音：这份登录态到底是哪个账号。"""
    ck = ''
    p = os.path.join(HERE, 'cookie.txt')
    if os.path.exists(p):
        try:
            ck = open(p, encoding='utf-8').read().strip()
        except Exception:
            ck = ''
    if not ck:
        ck = (os.environ.get('ROBOTLIVE_COOKIE') or '').strip()
    if not ck:
        return {'ok': False, 'msg': '没有 cookie.txt —— 现在是游客态，收不到礼物消息'}

    import urllib.request
    req = urllib.request.Request(
        'https://live.douyin.com/webcast/user/me/?aid=6383',
        headers={
            'Cookie': ck,
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/131.0.0.0 Safari/537.36'),
            'Referer': 'https://live.douyin.com/',
        })
    # 本机有系统代理，这里必须绕过，否则请求会走代理失败
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=6) as r:
            data = json.loads(r.read().decode('utf-8', 'replace'))
    except Exception as e:
        return {'ok': False, 'msg': f'查询账号失败：{type(e).__name__}'}
    u = (data or {}).get('data') or {}
    if not u.get('id'):
        return {'ok': False, 'msg': 'cookie 已失效（接口没返回账号），需要重新扫码'}
    return {'ok': True, 'name': u.get('nickname') or '（未命名）',
            'uid': str(u.get('id')), 'short_id': str(u.get('short_id') or '')}


def _who_loop():
    """后台每 60 秒刷一次，避免拖慢页面 2 秒一轮的状态查询。"""
    while True:
        try:
            _WHO['data'] = _who_fetch()
        except Exception:
            pass
        time.sleep(60)


def whoami():
    return _WHO['data'] or {'ok': None, 'msg': '查询中…'}


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><title>RobotLive 控制台</title>
<style>
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
     background:#f5f6f8;color:#1f2328}
header{background:#fff;border-bottom:1px solid #e3e6ea;padding:14px 22px;
       display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:9}
h1{font-size:16px;margin:0;font-weight:600}
.dot{width:8px;height:8px;border-radius:50%;background:#c9ced6}
.dot.on{background:#2ea043;box-shadow:0 0 0 3px #2ea04322}
.muted{color:#6b7280;font-size:12px}
.wrap{max-width:1100px;margin:0 auto;padding:20px}
.tabs{display:flex;gap:6px;margin-bottom:16px}
.tab{padding:8px 16px;border-radius:8px;background:#fff;border:1px solid #e3e6ea;
     cursor:pointer;font-size:14px}
.tab.active{background:#1f6feb;color:#fff;border-color:#1f6feb}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:12px;padding:18px;margin-bottom:16px}
.card h2{font-size:14px;margin:0 0 12px;font-weight:600;color:#374151}
label{display:block;font-size:12px;color:#6b7280;margin-bottom:4px}
input,select,textarea{width:100%;padding:8px 10px;border:1px solid #d0d7de;border-radius:8px;
       font:inherit;background:#fff}
textarea{resize:vertical;min-height:64px;font-family:inherit}
.row{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}
.row>div{flex:1;min-width:140px}
button{padding:9px 18px;border-radius:8px;border:1px solid #d0d7de;background:#fff;
       cursor:pointer;font:inherit}
button.primary{background:#1f6feb;color:#fff;border-color:#1f6feb}
button.danger{background:#fff;color:#cf222e;border-color:#f0b6b6}
button:hover{filter:brightness(.97)}
#log{background:#0d1117;color:#c9d1d9;font:12px/1.7 Consolas,Menlo,monospace;
     border-radius:10px;padding:12px;height:260px;overflow:auto;white-space:pre-wrap}
.tier{border:1px solid #e3e6ea;border-radius:10px;padding:12px;margin-bottom:10px;background:#fbfcfd}
.tier .hd{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.tier .hd b{font-size:13px}
.ov{border:1px solid #ffe0b2;background:#fffaf2;border-radius:10px;padding:12px;margin-bottom:10px}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;background:#eef2f7;
     font-size:11px;color:#57606a;margin-left:6px}
.hint{font-size:12px;color:#6b7280;margin-top:6px}
.stat{display:flex;gap:22px;flex-wrap:wrap;margin-top:6px}
.stat div b{font-size:20px;display:block}
.hide{display:none}
/* 多说法编辑器 */
.vrow{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.vno{flex:0 0 22px;height:22px;line-height:22px;text-align:center;border-radius:50%;
     background:#eef2f7;color:#57606a;font-size:11px}
.vrow input{flex:1}
.vrow button{padding:2px 9px;border-radius:6px;font-size:14px;line-height:1.2}
.mini{padding:5px 12px;font-size:12px;border-radius:7px}
.ov .hd{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.ov .hd b{font-size:13px}
.ov .hd .cnt{font-size:12px;color:#b45309;background:#fff3e0;border:1px solid #ffe0b2;
     border-radius:999px;padding:1px 9px}
.say-out{font-size:12px;color:#1f6feb;margin-top:4px}
/* 实时弹幕列表 */
#chatlist{max-height:280px;overflow:auto;border:1px solid #eef1f4;border-radius:8px}
.chatrow{display:flex;align-items:center;gap:8px;padding:7px 10px;font-size:13px;
     border-bottom:1px solid #f2f4f7}
.chatrow:last-child{border-bottom:0}
.chatrow .tm{flex:0 0 62px;color:#9aa3ae;font-size:11px}
.chatrow .nm{flex:0 0 96px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
     font-weight:600;color:#374151}
.chatrow .tx{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chatrow.wake .tx{color:#b45309}
.chatrow button{flex:0 0 auto;padding:4px 10px;font-size:12px;border-radius:6px}
.chatempty{padding:14px;color:#9aa3ae;font-size:12px;text-align:center}
/* 自我介绍 */
.irow{display:flex;gap:10px;align-items:flex-start;margin-bottom:8px}
.irow input[type=radio]{flex:0 0 auto;width:auto;margin-top:15px}
.irow textarea{flex:1;min-height:54px}
/* 知识库 */
.kbitem{border:1px solid #e5e8ec;border-radius:8px;padding:8px 10px;margin-bottom:8px;background:#fff}
.kbitem summary{cursor:pointer;font-size:13px;line-height:1.7}
.kbody{margin-top:10px}
.kbody textarea{width:100%;min-height:64px}
.kbsug{border:1px solid #e5e8ec;border-radius:8px;padding:10px;margin-top:10px;background:#fbfcfd}
.kbsug textarea{width:100%;min-height:52px;margin-top:6px}
.kbsug .row{margin-top:8px}
</style></head><body>
<header>
  <span class="dot" id="dot"></span>
  <h1>RobotLive 控制台</h1>
  <span class="muted" id="hdr">未运行</span>
  <span class="muted" style="margin-left:auto;font-size:11px" id="ver"></span>
</header>
<div class="wrap">
  <div class="tabs">
    <div class="tab active" data-t="room">直播间</div>
    <div class="tab" data-t="gift">礼物反馈</div>
    <div class="tab" data-t="reply">弹幕回复</div>
    <div class="tab" data-t="kb">知识库</div>
    <div class="tab" data-t="debug">调试工具</div>
  </div>

  <div id="tab-room">
    <div class="card">
      <h2>检测的直播间</h2>
      <div class="row">
        <div style="flex:2">
          <label>直播间号或链接（live.douyin.com 后面那串数字）</label>
          <input id="room" placeholder="例如 705704016990">
        </div>
        <div style="flex:0">
          <button class="primary" onclick="startRun()">开始检测</button>
        </div>
        <div style="flex:0">
          <button class="danger" onclick="stopRun()">停止</button>
        </div>
      </div>
      <div class="hint">支持直接粘贴完整链接，会自动提取数字。运行状态每 2 秒刷新。</div>
      <div class="row" style="margin-top:8px">
        <div style="flex:0 0 auto">
          <button id="room-auto" onclick="detectRoom()">识别我自己的直播间号</button>
        </div>
        <div style="flex:1"><span class="muted" id="room-msg"></span></div>
      </div>
      <div class="hint">换号直播时点这个：它会用当前 Cookie 打开你的抖音主页，把正在直播的那个直播间号抓回来填好。</div>
      <div class="hint" id="who" style="margin-top:6px"></div>
      <div class="row" style="margin-top:12px">
        <div style="flex:3"><label>试一下机器人（验证隧道与语音是否通）</label>
          <input id="say" value="播报链路自检，我在这里。"></div>
        <div style="flex:0"><button onclick="doSay()">让机器人说</button></div>
      </div>
      <div id="say-msg" class="hint"></div>
    </div>

    <div class="card">
      <h2>抖音 Cookie <span class="muted" id="ck-state">读取中…</span></h2>
      <div class="row">
        <div style="flex:0 0 auto"><button class="primary" id="ck-auto">用当前账号获取</button></div>
        <div style="flex:0 0 auto"><button id="ck-fresh">换号登录</button></div>
        <div style="flex:1"><span class="muted" id="ck-auto-msg">换号直播时点「换号登录」：窗口里登录新号，自动抓 Cookie 并接上新直播间</span></div>
      </div>
      <div class="hint">会单独开一个干净的浏览器窗口（不动你平时的浏览器），扫码后自动抓取、写入配置、重启采集。
        <b>换号</b>用右边那个按钮，它会先退出当前账号，否则窗口一打开就抓到旧号、几秒就关了。</div>
      <div style="margin:14px 0 6px"><span class="muted">或者手动粘贴（自动那条走不通时用）：</span></div>
      <textarea id="ck-raw" style="min-height:72px"
        placeholder="把 Cookie 粘这里。最省事的拿法：F12 → Network → 刷新页面 → 点任意一个 douyin.com 的请求 → Request Headers → Cookie → 复制那串值。也支持整条 curl 命令或带 Cookie: 前缀的文本。"></textarea>
      <div class="row" style="margin-top:10px">
        <div style="flex:0 0 auto"><button class="primary" id="ck-save">校验并更新</button></div>
        <div style="flex:0 0 auto"><span class="muted" id="ck-msg"></span></div>
      </div>
      <div class="hint">先校验有没有 <b>sessionid</b>（登录态的关键字段，缺它等于游客，收不到礼物）。
        通过后自动写进 <code>bin/config.yaml</code> 并重启采集服务；正在检测的话会中断约 5 秒再自动接上。</div>
    </div>

    <div class="card">
      <h2>实时状态</h2>
      <div class="stat">
        <div><b id="c-gift">0</b><span class="muted">礼物</span></div>
        <div><b id="c-speak">0</b><span class="muted">已开口</span></div>
        <div><b id="c-chat">0</b><span class="muted">弹幕</span></div>
        <div><b id="c-enter">0</b><span class="muted">进场</span></div>
        <div><b id="c-reply">0</b><span class="muted">弹幕回复</span></div>
      </div>
      <div style="margin-top:14px"><div id="log">（等待启动）</div></div>
    </div>
  </div>

  <div id="tab-gift" class="hide">
    <div class="card">
      <h2>点赞与关注 <span class="tag">每人只谢一次</span>
        <span class="muted" id="i-msg"></span></h2>
      <div class="row">
        <div><label>点赞</label>
          <select id="i-like-on"><option value="1">开启</option><option value="0">关闭</option></select></div>
        <div><label>关注</label>
          <select id="i-follow-on"><option value="1">开启</option><option value="0">关闭</option></select></div>
        <div><label>点赞最小间隔（秒）</label>
          <input type="number" id="i-gap" min="0" step="0.5" value="2"></div>
        <div style="flex:0 0 auto;align-self:flex-end">
          <button class="primary" id="i-save">保存</button></div>
      </div>
      <div class="row" style="margin-top:10px">
        <div><label>点赞时念（每行一句，{user} 是昵称）</label>
          <textarea id="i-like" style="min-height:104px"></textarea></div>
        <div><label>关注时念</label>
          <textarea id="i-follow" style="min-height:104px"></textarea></div>
      </div>
      <div class="hint">每类写 <b>三句</b>就行，每次事件<b>随机挑一句</b>，且不会连着两次说同一句
        —— 和礼物话术完全同一套选法。<br>
        每个人只谢<b>一次</b>：他点一百下赞也只回一句，关注同理，不会刷屏。<br>
        一批新人同时点赞时，间隔内的会被跳过（日志里显示「太密集，这次先跳过」），
        宁可少谢一个，也不让礼物道谢被挤到后面。改完保存立即生效，不用重启。</div>
    </div>

    <div class="card">
      <h2>指定礼物专属反馈<span class="tag">优先于钻石档位</span></h2>
      <div id="ovs"></div>
      <div class="row" style="margin-top:10px">
        <div><label>礼物名称（要和抖音里显示的一致）</label>
          <input id="newgift" placeholder="例如 玫瑰 / 人气票 / 小心心"></div>
        <div style="flex:0"><button onclick="addOverride()">添加礼物</button></div>
      </div>
      <div class="hint">留空表示不加。填写后，这个礼物只走这里的话术，钻石档位不再生效。
        写<b>多句</b>时每次事件<b>随机挑一句</b>，且不会连着两次说同一句（和点赞/关注同一套选法）。</div>
    </div>

    <div class="card">
      <h2>按钻石数分档（没有专属设置的礼物走这里）</h2>
      <div class="hint" style="margin:0 0 10px">每档话术写<b>多句</b>就行：收到礼物时<b>随机挑一句</b>，
        且不会连着两次说同一句（和点赞/关注、专属话术同一套选法）。</div>
      <div id="tiers"></div>
      <button onclick="addTier()">添加档位</button>
    </div>

    <div class="card">
      <h2>试听效果</h2>
      <div class="row">
        <div><label>礼物</label><input id="p-gift" value="玫瑰"></div>
        <div><label>钻石数</label><input id="p-diamond" type="number" value="1"></div>
        <div><label>观众昵称</label><input id="p-user" value="小明"></div>
        <div><label>数量</label><input id="p-count" type="number" value="1"></div>
        <div style="flex:0"><button onclick="doPreview()">生成一句</button></div>
      </div>
      <div id="p-out" style="margin-top:12px;font-size:15px"></div>
    </div>

    <div class="card">
      <button class="primary" onclick="saveGifts()">保存（立即生效，不用重启）</button>
      <span id="save-msg" class="muted" style="margin-left:10px"></span>
    </div>
  </div>

  <div id="tab-reply" class="hide">
    <div class="card">
      <h2>实时弹幕 <span class="muted">（点「让远仔回」就让它答这一条，不用等唤醒词）</span></h2>
      <div id="chatlist"><div class="chatempty">还没有弹幕。开播后这里会实时出现。</div></div>
      <div class="hint">点选回复不受唤醒词和 5 分钟冷却限制，但仍然排在同一条队列里，
        不会和礼物话术抢麦。队列满时会提示稍后再点。</div>
    </div>

    <div class="card">
      <h2>自我介绍 <span class="muted">（准备三段，挑一段让它说）</span></h2>
      <div>
        <div class="irow"><input type="radio" name="intro" value="0" checked>
          <textarea id="intro0" placeholder="第一段自我介绍"></textarea></div>
        <div class="irow"><input type="radio" name="intro" value="1">
          <textarea id="intro1" placeholder="第二段自我介绍"></textarea></div>
        <div class="irow"><input type="radio" name="intro" value="2">
          <textarea id="intro2" placeholder="第三段自我介绍"></textarea></div>
      </div>
      <div class="row" style="margin-top:10px">
        <div style="flex:0 0 auto"><button class="primary" id="intro-say">让远仔说选中的这段</button></div>
        <div style="flex:0 0 auto"><span class="muted" id="intro-msg"></span></div>
      </div>
      <div class="hint">点按钮是<b>你主动让它开场</b>，不打断正在说的话，会排队播。
        另外弹幕里有人问「自我介绍 / 介绍一下你自己」时，会自动从这三段里随机挑一段（不连着重复）。
        改完记得点页面下方的「保存（立即生效，不用重启）」。</div>
    </div>

    <div class="card">
      <h2>唤醒与限流</h2>
      <div class="row">
        <div style="flex:0"><label>启用</label>
          <select id="r-enable">
            <option value="1">开</option><option value="0">关</option></select></div>
        <div><label>唤醒标记（弹幕里带这个才回）</label><input id="r-marker" placeholder="#远仔"></div>
        <div><label>唤醒词（不带 # 也叫得应，逗号分隔）</label>
          <input id="r-wake" placeholder="远仔"></div>
        <div style="flex:0 0 110px"><label>最少连喊几遍</label>
          <input type="number" id="r-wakemin" min="1" max="4" value="2"></div>
        <div><label>念之前加的话（用 {user} 名字、{question} 问题）</label>
          <input id="r-prefix" placeholder="{user}问，{question}。"></div>
      </div>
      <div class="row" style="margin-top:10px">
        <div><label>同一个人冷却（分钟）</label><input type="number" id="r-usercd" min="0"></div>
        <div><label>排队上限（条）</label><input type="number" id="r-pending" min="1"></div>
        <div><label>排队超时作废（秒）</label><input type="number" id="r-stale" min="5"></div>
        <div><label>问句最多念（字）</label><input type="number" id="r-qmax" min="0"></div>
        <div><label>一次播报上限（字）</label><input type="number" id="r-speakmax" min="0"></div>
        <div><label>回答最长字数</label><input type="number" id="r-maxa"></div>
        <div><label>优先级 1-10</label><input type="number" id="r-prio"></div>
      </div>
      <div class="row" style="margin-top:10px">
        <div style="flex:0"><label>天气问答</label>
          <select id="r-weather"><option value="1">开</option><option value="0">关</option></select></div>
        <div><label>默认城市（弹幕没说城市时用它）</label><input id="r-city" placeholder="郑州"></div>
      </div>
      <div class="hint">回复是<b>串行</b>的：一个人问完、机器人把话说完，才轮到下一个人；
        同一人默认 5 分钟内只回一次（自己测试时把上面的冷却改成 0 分钟，不然会被自己挡住）。
        队列里等太久的会自动作废，不会去答三分钟前的问题。<br>
        叫它的方式：直播间指令用 <b>#远仔 问题</b>；不带 # 时喊两遍 <b>远仔远仔 问题</b> 也认。
        单次「远仔」不算，免得观众随口提到名字就被当成提问（想放开就把「唤醒词最少重复」调成 1）。<br>
        「念之前加的话」决定播报怎么开口：<b>{user}问，{question}。</b> 会念成
        「陈家人问，你多大了。」再接回答。旧写法只写 <b>{user}问，</b> 也会自动把问题补在后面。</div>
    </div>

    <div class="card">
      <h2>大模型（让弹幕问答像本体一样能聊）</h2>
      <div class="row">
        <div style="flex:0"><label>启用</label>
          <select id="r-llm"><option value="1">开</option><option value="0">关</option></select></div>
        <div><label>接口地址（OpenAI 兼容）</label>
          <input id="r-llmurl" placeholder="https://api.deepseek.com/v1"></div>
        <div><label>模型名</label><input id="r-llmmodel" placeholder="deepseek-chat"></div>
      </div>
      <div class="row" style="margin-top:10px">
        <div><label>API Key（只写不读，留空表示不改）<span id="r-key-state"
          class="muted"></span></label>
          <input id="r-llmkey" type="password" placeholder="sk-..."></div>
      </div>
      <div class="hint">预设命中的问题不走模型（零延迟零成本），没命中才问模型，模型失败自动退回兜底话术；
        天气类问题走本地实时接口，不花模型的钱。<br>
        <b>Key 只存在本机 secrets/llm_key，页面上不会回显。</b>
        DeepSeek 填 <code>https://api.deepseek.com/v1</code> + <code>deepseek-chat</code>；
        硅基流动填 <code>https://api.siliconflow.cn/v1</code>；智谱填 <code>https://open.bigmodel.cn/api/paas/v4</code>。</div>
    </div>

    <div class="card">
      <h2>预设问答（命中就不走模型，零延迟）</h2>
      <div class="hint" style="margin:0 0 8px">一行一条，格式：<b>关键词1,关键词2 | 回答</b>。回答里可用 {user} 占位。</div>
      <textarea id="r-presets" style="min-height:170px"
        placeholder="多大,几岁 | 我今年才刚满一岁。"></textarea>
      <div style="margin-top:10px"><label>没命中时的兜底（一行一句，随机挑）</label>
        <textarea id="r-fallback" style="min-height:70px"></textarea></div>
      <div style="margin-top:12px">
        <button class="primary" onclick="saveReplies()">保存（立即生效，不用重启）</button>
        <span id="r-save-msg" class="muted" style="margin-left:10px"></span>
      </div>
    </div>

    <div class="card">
      <h2>试一句（只算不说，不动机器人）</h2>
      <div class="row">
        <div style="flex:3"><label>模拟一条弹幕</label><input id="r-test" value="#远仔 你多大了"></div>
        <div style="flex:0"><button onclick="testReply()">试算</button></div>
      </div>
      <div id="r-out" class="hint"></div>
    </div>
  </div>

  <div id="tab-kb" class="hide">
    <div class="card">
      <h2>高频问题 <span class="tag">问够次数才学</span></h2>
      <div class="row">
        <div><label>学习</label>
          <select id="kb-learn"><option value="1">开启</option><option value="0">关闭</option></select></div>
        <div><label>达几次生成建议</label><input id="kb-min" type="number" value="3"></div>
        <div><label>相似阈值（0~1）</label><input id="kb-thr" type="number" step="0.01" value="0.72"></div>
        <div><label>建议冷却（分钟）</label><input id="kb-cd" type="number" value="30"></div>
        <div style="flex:0"><button class="primary" onclick="saveKbSettings()">保存设置</button></div>
      </div>
      <div class="hint" id="kb-learn-msg"></div>
      <div id="kb-suggest"></div>
      <div class="hint">同一问题被问够次数后，会用已配置的大模型生成一版更巧妙的回答；采纳后才生效。
        生成时会把问题样本和相关知识发给模型，关闭「学习」后不再记录也不再生成。</div>
    </div>

    <div class="card">
      <h2>知识条目 <span class="tag">来自 knowledge.md</span></h2>
      <div class="row">
        <div style="flex:2"><label>搜索</label>
          <input id="kb-search" placeholder="标题 / 关键词 / 正文" oninput="renderKb()"></div>
        <div style="flex:0 0 auto"><label>&nbsp;</label>
          <button onclick="loadKb()">重新加载</button></div>
      </div>
      <div class="hint">直接编辑项目根目录的 <code>knowledge.md</code>，保存后 200ms 内生效，这里只做浏览。
        faq=能直接念的回答；fact=产品资料，只给大模型参考；persona=语气人设。</div>
      <div id="kb-list" style="margin-top:10px"></div>
      <div class="hint" id="kb-msg"></div>
    </div>
  </div>

  <div id="tab-debug" class="hide">
    <div class="card">
      <h2>实时消息概览</h2>
      <div class="stat">
        <div><b id="d-chat">0</b><span class="muted">弹幕</span></div>
        <div><b id="d-gift">0</b><span class="muted">礼物</span></div>
        <div><b id="d-enter">0</b><span class="muted">进场</span></div>
        <div><b id="d-speak">0</b><span class="muted">已开口</span></div>
      </div>
      <div style="margin-top:12px">
        <label>最近 10 条消息</label>
        <div id="recent" style="background:#f6f8fa;border-radius:8px;padding:10px;
             font:12px/1.9 Consolas,Menlo,monospace;min-height:120px;max-height:260px;overflow:auto"></div>
      </div>
      <div class="hint">数据来自正在运行的采集：弹幕 / 礼物 / 进场，按到达顺序倒序显示。</div>
    </div>

    <div class="card">
      <h2>手动模拟发送<span class="tag">走完整规则→播报链路</span></h2>
      <div class="row">
        <div><label>类型</label>
          <select id="sim-kind" onchange="simKind()">
            <option value="gift">礼物</option>
            <option value="chat">弹幕</option>
          </select></div>
        <div><label>用户昵称</label><input id="sim-user" value="测试用户"></div>
        <div id="sim-gift-wrap"><label>礼物名称</label>
          <input id="sim-gift" value="玫瑰"></div>
        <div id="sim-chat-wrap" class="hide"><label>弹幕内容</label>
          <input id="sim-text" value="主播你好，它会跳舞吗"></div>
        <div><label>数量</label><input id="sim-count" type="number" value="1"></div>
        <div style="flex:0"><button class="primary" onclick="doSim()">发送模拟</button></div>
      </div>
      <div id="sim-out" style="margin-top:12px"></div>
      <div class="hint">模拟消息的结构和真实采集完全一致，不会另写一套逻辑；发送后机器人会真的开口。</div>
    </div>

    <div class="card">
      <h2>礼物全字段日志</h2>
      <div class="row">
        <div style="flex:0">
          <label style="display:flex;align-items:center;gap:8px;margin:0">
            <input type="checkbox" id="dbgflag" style="width:auto" onchange="saveFlag()">
            <span style="font-size:13px;color:#1f2328">开启（打印礼物全字段 + 规则命中）</span>
          </label>
        </div>
      </div>
      <div class="hint">开启后：每条礼物打印昵称/UID/礼物ID/单价/数量/总价/连击，并标注普通·粉丝团·盲盒；
        同时打印规则命中结果。调试完关掉即可恢复简洁输出，200ms 内生效。</div>
    </div>

    <div class="card">
      <h2>采集自检</h2>
      <div class="hint">启动采集时会自动检查三项：采集服务连接、直播间是否开播、消息是否持续到达。
        结果直接出现在「直播间」页签的日志里，以 <code>[自检]</code> 开头。</div>
      <div class="row" style="margin-top:10px">
        <div style="flex:0"><button onclick="jumpLog()">去看日志</button></div>
      </div>
    </div>
  </div>
</div>

<script>
let CFG = null;

async function api(path, body){
  const opt = body ? {method:'POST',headers:{'Content-Type':'application/json'},
                      body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  return await r.json();
}

document.querySelectorAll('.tab').forEach(t=>{
  t.onclick = ()=>{
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    t.classList.add('active');
    ['room','gift','reply','kb','debug'].forEach(k=>{
      document.getElementById('tab-'+k).classList.toggle('hide', k!==t.dataset.t);
    });
    if(t.dataset.t==='gift'){ loadGifts(); loadInteractions(); }
    if(t.dataset.t==='reply') loadReplies();
    if(t.dataset.t==='kb') loadKb();
    if(t.dataset.t==='debug') { refresh(); loadFlag(); }
  };
});

async function loadFlag(){
  const c = await api('/api/gifts');
  document.getElementById('dbgflag').checked = !!c.debug_gift_full_log;
}

async function saveFlag(){
  const c = await api('/api/gifts');
  c.debug_gift_full_log = document.getElementById('dbgflag').checked;
  await api('/api/gifts', c);
  refresh();
}

function simKind(){
  const g = document.getElementById('sim-kind').value==='gift';
  document.getElementById('sim-gift-wrap').classList.toggle('hide', !g);
  document.getElementById('sim-chat-wrap').classList.toggle('hide', g);
}

function jumpLog(){
  document.querySelector('.tab[data-t="room"]').click();
}

function renderRecent(lines){
  const out = [];
  for(const l of lines){
    if(/弹幕 /.test(l) || /收到礼物 /.test(l) || /进场 /.test(l) || /播报/.test(l)) out.push(l.trim());
  }
  const box = document.getElementById('recent');
  if(!out.length){ box.textContent = '（还没有消息）'; return; }
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
  box.textContent = out.slice(-10).reverse().join('\n');
  if(atBottom) box.scrollTop = 0;
}

async function doSim(){
  const kind = document.getElementById('sim-kind').value;
  const body = {kind, user: document.getElementById('sim-user').value,
                count: +document.getElementById('sim-count').value};
  if(kind==='gift') body.gift = document.getElementById('sim-gift').value;
  else body.text = document.getElementById('sim-text').value;
  const el = document.getElementById('sim-out');
  el.innerHTML = '<span class="muted">发送中…</span>';
  const r = await api('/api/simulate', body);
  if(kind==='chat'){
    el.innerHTML = '<div class="hint">' + esc(r.msg||'') + '</div>';
  } else if(r.ok){
    el.innerHTML = `<div style="padding:12px;background:#f0fff4;border:1px solid #b7ebc6;border-radius:8px">
      「${esc(r.text)}」</div>
      <div class="hint">命中：${esc(r.tier)} · 优先级 ${r.priority} · 机器人 ${r.elapsed_ms} ms 受理</div>`;
  } else {
    el.innerHTML = `<div style="padding:12px;background:#fff5f5;border:1px solid #ffc9c9;border-radius:8px">
      失败：${esc(r.msg || r.error || '未知原因')}</div>`;
  }
  refresh();
}

function extractRoom(s){
  s = (s||'').trim();
  const m = s.match(/(\d{6,})/);
  return m ? m[1] : s;
}

async function refresh(){
  const s = await api('/api/state');
  renderAuto(s.auto);
  document.getElementById('dot').className = 'dot' + (s.running?' on':'');
  document.getElementById('ver').textContent = '界面 ' + (s.boot||'');
  document.getElementById('hdr').textContent = s.running
    ? ('运行中 · 直播间 ' + s.room) : '未运行';
  if(!document.getElementById('room').value) document.getElementById('room').value = s.room||'';
  const w = s.who || {};
  const wEl = document.getElementById('who');
  if(wEl){
    if(w.ok === true){
      wEl.textContent = '采集账号：' + w.name + '（uid ' + w.uid + '）';
      wEl.style.color = '';
    }else if(w.ok === false){
      wEl.textContent = '采集账号：' + (w.msg || '未知');
      wEl.style.color = '#c0392b';
    }else{
      wEl.textContent = '采集账号：查询中…';
    }
  }
  document.getElementById('c-gift').textContent = s.counts.gift;
  document.getElementById('c-speak').textContent = s.counts.speak;
  document.getElementById('c-chat').textContent = s.counts.chat;
  document.getElementById('c-enter').textContent = s.counts.enter;
  document.getElementById('c-reply').textContent = s.counts.reply || 0;
  const dc = document.getElementById('d-chat');
  if(dc){
    dc.textContent = s.counts.chat;
    document.getElementById('d-gift').textContent = s.counts.gift;
    document.getElementById('d-enter').textContent = s.counts.enter;
    document.getElementById('d-speak').textContent = s.counts.speak;
    renderRecent(s.log);
  }
  const log = document.getElementById('log');
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  log.textContent = s.log.length ? s.log.join('\n') : '（等待启动）';
  if(atBottom) log.scrollTop = log.scrollHeight;
  renderChats(s.chats);
}

let CHAT_CACHE = [];
const SENT = {};            // 记录刚点过的弹幕，避免 2 秒刷新把「已排队」冲掉
function renderChats(list){
  CHAT_CACHE = list || [];
  const el = document.getElementById('chatlist');
  if(!el) return;
  if(!CHAT_CACHE.length){
    el.innerHTML = '<div class="chatempty">还没有弹幕。开播后这里会实时出现。</div>';
    return;
  }
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
  const rows = CHAT_CACHE.slice().reverse().map(c=>{
    const wake = /(#|＃)远仔|远仔远仔/.test(c.text) ? ' wake' : '';
    const sent = SENT[c.id] && (Date.now() - SENT[c.id] < 5000);
    return '<div class="chatrow' + wake + '">'
      + '<span class="tm">' + esc(c.t) + '</span>'
      + '<span class="nm">' + esc(c.name) + '</span>'
      + '<span class="tx" title="' + esc(c.text) + '">' + esc(c.text) + '</span>'
      + '<button class="mini" data-id="' + esc(String(c.id)) + '"'
      + (sent ? ' disabled' : '') + '>' + (sent ? '已排队' : '让远仔回') + '</button>'
      + '</div>';
  }).join('');
  el.innerHTML = rows;
  if(atBottom) el.scrollTop = el.scrollHeight;
  el.querySelectorAll('button[data-id]').forEach(b=>{
    b.onclick = ()=> replyChat(b.dataset.id, b);
  });
}

async function loadInteractions(){
  const c = await api('/api/interactions');
  const l = c.like || {}, f = c.follow || {};
  document.getElementById('i-like-on').value = (l.enabled === false ? '0' : '1');
  document.getElementById('i-follow-on').value = (f.enabled === false ? '0' : '1');
  const gap = (l.min_gap_ms != null ? l.min_gap_ms : 2000) / 1000;
  document.getElementById('i-gap').value = Math.round(gap * 10) / 10;
  document.getElementById('i-like').value = (l.thanks || []).join('\n');
  document.getElementById('i-follow').value = (f.thanks || []).join('\n');
}

async function saveInteractions(){
  const msg = document.getElementById('i-msg');
  const btn = document.getElementById('i-save');
  const body = {
    like: {
      enabled: document.getElementById('i-like-on').value === '1',
      min_gap_ms: Math.round((+document.getElementById('i-gap').value || 0) * 1000),
      thanks: document.getElementById('i-like').value.split('\n')
        .map(x=>x.trim()).filter(Boolean)
    },
    follow: {
      enabled: document.getElementById('i-follow-on').value === '1',
      thanks: document.getElementById('i-follow').value.split('\n')
        .map(x=>x.trim()).filter(Boolean)
    }
  };
  btn.disabled = true;
  const r = await api('/api/interactions', body);
  btn.disabled = false;
  msg.textContent = r.ok ? ('已保存 ' + new Date().toLocaleTimeString() + '，立即生效')
                         : ('失败：' + (r.msg || ''));
  msg.style.color = r.ok ? '' : '#c62828';
}

const iSaveBtn = document.getElementById('i-save');
if(iSaveBtn) iSaveBtn.onclick = saveInteractions;

async function replyChat(id, btn){
  const c = CHAT_CACHE.find(x=>String(x.id) === String(id));
  if(!c){ return; }
  if(btn){ btn.disabled = true; btn.textContent = '排队中…'; }
  const r = await api('/api/reply-danmaku', {name: c.name, text: c.text});
  if(r.ok){ SENT[c.id] = Date.now(); }
  if(btn){
    btn.textContent = r.ok ? '已排队' : (r.msg || '失败');
    if(!r.ok){
      btn.disabled = false;
      setTimeout(()=>{ btn.textContent = '让远仔回'; }, 2000);
      alert('没排上：' + (r.msg || '未知原因'));
    }
  }
  refresh();
}

async function startRun(){
  const room = extractRoom(document.getElementById('room').value);
  if(!room){ alert('先填直播间号'); return; }
  const r = await api('/api/start', {room});
  if(!r.ok) alert(r.msg||'启动失败');
  refresh();
}
async function stopRun(){ await api('/api/stop', {}); refresh(); }

async function doSay(){
  const text = document.getElementById('say').value;
  const el = document.getElementById('say-msg');
  el.textContent = '发送中…';
  const r = await api('/api/say', {text});
  el.textContent = r.ok
    ? ('已受理，用时 ' + (r.result && r.result.elapsed_ms) + ' ms')
    : ('失败：' + (r.msg || JSON.stringify(r.result||{})));
}

async function loadGifts(){
  CFG = await api('/api/gifts');
  renderTiers(); renderOverrides();
}

function renderTiers(){
  const wrap = document.getElementById('tiers');
  wrap.innerHTML = '';
  CFG.tiers.forEach((t,i)=>{
    const d = document.createElement('div');
    d.className='tier';
    d.innerHTML = `
      <div class="hd"><b>${t.name||'档位'+(i+1)}</b>
        <button class="danger" onclick="delTier(${i})">删除</button></div>
      <div class="row">
        <div><label>名称</label><input value="${esc(t.name)}" oninput="CFG.tiers[${i}].name=this.value"></div>
        <div><label>最小钻石</label><input type="number" value="${t.min_diamond}" oninput="CFG.tiers[${i}].min_diamond=+this.value"></div>
        <div><label>最大钻石</label><input type="number" value="${t.max_diamond}" oninput="CFG.tiers[${i}].max_diamond=+this.value"></div>
        <div><label>优先级 1-10</label><input type="number" value="${t.priority}" oninput="CFG.tiers[${i}].priority=+this.value"></div>
        <div><label>打断当前播报</label>
          <select onchange="CFG.tiers[${i}].interrupt=(this.value==='1')">
            <option value="1" ${t.interrupt?'selected':''}>是</option>
            <option value="0" ${!t.interrupt?'selected':''}>否</option></select></div>
      </div>
      <div style="margin-top:8px"><label>话术（一行一句，{user} 昵称，{gift} 礼物，{count} 数量）</label>
        <textarea oninput="CFG.tiers[${i}].templates=this.value.split('\\n').filter(x=>x.trim())">${esc((t.templates||[]).join('\n'))}</textarea></div>`;
    wrap.appendChild(d);
  });
}

function renderOverrides(){
  const wrap = document.getElementById('ovs');
  wrap.innerHTML = '';
  const ovs = CFG.gift_overrides || {};
  const keys = Object.keys(ovs);
  if(!keys.length){ wrap.innerHTML = '<div class="muted">还没有专属设置。下面添加一个试试。</div>'; return; }

  keys.forEach((k, ki)=>{
    const o = ovs[k];
    const tpls = (o.templates && o.templates.length) ? o.templates.slice() : [''];
    const d = document.createElement('div');
    d.className = 'ov';
    const rows = tpls.map((t, ti)=>`
      <div class="vrow">
        <span class="vno">${ti+1}</span>
        <input value="${esc(t)}" placeholder="第 ${ti+1} 种说法" data-k="${ki}" data-i="${ti}">
        <button class="danger" data-del="${ki}:${ti}" title="删掉这一句">×</button>
      </div>`).join('');
    d.innerHTML = `
      <div class="hd"><b>${esc(k)}</b>
        <span class="cnt">${tpls.length} 种说法 · 随机挑一句，不连重</span>
        <button class="danger" style="margin-left:auto" data-rm="${ki}">删除这个礼物</button></div>
      <div class="row">
        <div><label>标签（可空）</label><input value="${esc(o.label||'')}" data-f="label" data-k="${ki}"></div>
        <div><label>优先级 1-10</label><input type="number" value="${o.priority||8}" data-f="priority" data-k="${ki}"></div>
        <div><label>打断当前播报</label>
          <select data-f="interrupt" data-k="${ki}">
            <option value="1" ${o.interrupt!==false?'selected':''}>是</option>
            <option value="0" ${o.interrupt===false?'selected':''}>否</option></select></div>
      </div>
      <div style="margin-top:10px">
        <label>专属说法（收到这个礼物时，从下面随机抽一句念）</label>
        ${rows}
        <button class="mini" data-add="${ki}">+ 再加一种说法</button>
        <button class="mini" data-try="${ki}">随机试听</button>
        <div class="say-out" data-out="${ki}"></div>
      </div>`;
    wrap.appendChild(d);
  });

  const keyAt = i => Object.keys(CFG.gift_overrides || {})[i];

  wrap.querySelectorAll('input[data-i]').forEach(el=>{
    el.oninput = ()=>{
      const o = CFG.gift_overrides[keyAt(+el.dataset.k)];
      if(!o) return;
      o.templates = o.templates || [];
      o.templates[+el.dataset.i] = el.value;
    };
  });
  wrap.querySelectorAll('input[data-f],select[data-f]').forEach(el=>{
    const h = ()=>{
      const o = CFG.gift_overrides[keyAt(+el.dataset.k)];
      if(!o) return;
      const f = el.dataset.f;
      o[f] = (f==='priority') ? (+el.value || 8)
           : (f==='interrupt') ? (el.value === '1') : el.value;
    };
    el.oninput = h; el.onchange = h;
  });
  wrap.querySelectorAll('[data-del]').forEach(b=>{
    b.onclick = ()=>{
      const parts = b.dataset.del.split(':').map(Number);
      const o = CFG.gift_overrides[keyAt(parts[0])];
      if(!o || !o.templates) return;
      if(o.templates.length <= 1){ alert('至少留一种说法'); return; }
      o.templates.splice(parts[1], 1);
      renderOverrides();
    };
  });
  wrap.querySelectorAll('[data-add]').forEach(b=>{
    b.onclick = ()=>{
      const o = CFG.gift_overrides[keyAt(+b.dataset.add)];
      if(!o) return;
      o.templates = (o.templates || []).concat(['']);
      renderOverrides();
    };
  });
  wrap.querySelectorAll('[data-rm]').forEach(b=>{
    b.onclick = ()=>{
      const k = keyAt(+b.dataset.rm);
      if(confirm('删掉「' + k + '」的专属设置？（会自动走钻石档位兜底）')) delOverride(k);
    };
  });
  wrap.querySelectorAll('[data-try]').forEach(b=>{
    b.onclick = async ()=>{
      const ki = +b.dataset.try, key = keyAt(ki);
      const o = CFG.gift_overrides[key] || {};
      const pool = (o.templates || []).filter(x=>(x||'').trim());
      const out = wrap.querySelector('[data-out="' + ki + '"]');
      if(!pool.length){ if(out) out.textContent = '先写至少一句'; return; }
      const tpl = pool[Math.floor(Math.random() * pool.length)];
      const text = tpl.replace(/\{user\}/g, '小明')
                       .replace(/\{gift\}/g, key)
                       .replace(/\{count\}/g, '1');
      if(out) out.textContent = '抽到这句并已让机器人念：' + text;
      await api('/api/say', {text: text, priority: 6});
    };
  });
}

function ovSet(k, field, val){
  CFG.gift_overrides = CFG.gift_overrides || {};
  CFG.gift_overrides[k] = CFG.gift_overrides[k] || {};
  CFG.gift_overrides[k][field] = val;
}
function delOverride(k){
  delete CFG.gift_overrides[k]; renderOverrides();
}
function addOverride(){
  const g = document.getElementById('newgift').value.trim();
  if(!g){ alert('填个礼物名'); return; }
  CFG.gift_overrides = CFG.gift_overrides || {};
  CFG.gift_overrides[g] = {label:'专属', priority:9, interrupt:true,
    templates:[
      '谢谢{user}送的{gift}！这一份我记住了。',
      '哟，{user}的{gift}收到啦，比个心给你！',
      '{user}送的{gift}我看见了，谢啦，别破费呀！'
    ]};
  document.getElementById('newgift').value='';
  renderOverrides();
}
function addTier(){
  const last = CFG.tiers[CFG.tiers.length-1] || {max_diamond:0};
  CFG.tiers.push({name:'新档位', min_diamond:last.max_diamond+1,
    max_diamond:last.max_diamond+100, priority:8, interrupt:true,
    templates:['谢谢{user}的{gift}！']});
  renderTiers();
}
function delTier(i){ CFG.tiers.splice(i,1); renderTiers(); }

async function saveGifts(){
  const r = await api('/api/gifts', CFG);
  document.getElementById('save-msg').textContent = r.ok
    ? ('已保存 ' + new Date().toLocaleTimeString()) : ('失败: ' + (r.msg||''));
}

async function doPreview(){
  const body = {
    gift: document.getElementById('p-gift').value,
    diamond: +document.getElementById('p-diamond').value,
    user: document.getElementById('p-user').value,
    count: +document.getElementById('p-count').value,
    cfg: CFG
  };
  const r = await api('/api/preview', body);
  const tag = r.hit_override ? '专属话术' : ('档位：' + r.tier);
  document.getElementById('p-out').innerHTML =
    `<div style="padding:12px;background:#f6f8fa;border-radius:8px">「${esc(r.text)}」</div>
     <div class="hint">${tag} · 优先级 ${r.priority}</div>`;
}

async function loadReplies(){
  const c = await api('/api/replies');
  document.getElementById('r-enable').value = (c.enabled===false?'0':'1');
  document.getElementById('r-marker').value = c.marker || '#远仔';
  document.getElementById('r-wake').value = (c.wake_words||[]).join(',');
  document.getElementById('r-wakemin').value = (c.wake_min_repeat==null?2:c.wake_min_repeat);
  document.getElementById('r-prefix').value = c.speak_prefix || '';
  document.getElementById('r-usercd').value = Math.round((c.per_user_cooldown_ms||300000)/60000);
  document.getElementById('r-pending').value = c.max_pending || 6;
  document.getElementById('r-stale').value = Math.round((c.queue_stale_ms||60000)/1000);
  document.getElementById('r-qmax').value = (c.speak_question_max_chars==null?26:c.speak_question_max_chars);
  document.getElementById('r-speakmax').value = (c.max_speak_chars==null?64:c.max_speak_chars);
  document.getElementById('r-maxa').value = c.max_reply_chars || 60;
  document.getElementById('r-prio').value = c.priority || 6;
  const w = c.weather || {};
  document.getElementById('r-weather').value = (w.enabled===false?'0':'1');
  document.getElementById('r-city').value = w.default_city || '郑州';
  const L = c.llm || {};
  document.getElementById('r-llm').value = (L.enabled?'1':'0');
  document.getElementById('r-llmurl').value = L.base_url || 'https://api.deepseek.com/v1';
  document.getElementById('r-llmmodel').value = L.model || 'deepseek-chat';
  document.getElementById('r-llmkey').value = '';
  document.getElementById('r-key-state').textContent =
    c.has_llm_key ? '（已配置 Key）' : '（还没配置 Key）';
  const intros = (c.intros || []).slice(0, 3);
  [0, 1, 2].forEach(i=>{
    const el = document.getElementById('intro' + i);
    if(el) el.value = intros[i] || '';
  });
  document.getElementById('r-presets').value = (c.presets||[])
    .map(p => (p.keys||[]).join(',') + ' | ' + (p.reply||'')).join('\n');
  document.getElementById('r-fallback').value = (c.fallback||[]).join('\n');
  document.getElementById('r-save-msg').textContent = '';
}

function parsePresets(raw){
  const out = [];
  raw.split('\n').forEach(line => {
    line = line.trim();
    if(!line) return;
    const i = line.indexOf('|');
    if(i < 0) return;
    const keys = line.slice(0, i).split(/[,，]/).map(x=>x.trim()).filter(Boolean);
    const reply = line.slice(i+1).trim();
    if(keys.length && reply) out.push({keys: keys, reply: reply});
  });
  return out;
}

async function saveReplies(){
  const c = await api('/api/replies');
  c.enabled = document.getElementById('r-enable').value === '1';
  c.marker = document.getElementById('r-marker').value.trim();
  c.wake_words = document.getElementById('r-wake').value.split(/[,，、\s]+/)
    .map(x=>x.trim().replace(/^#/,'')).filter(Boolean);
  c.wake_min_repeat = Math.max(1, +document.getElementById('r-wakemin').value || 2);
  c.speak_prefix = document.getElementById('r-prefix').value;
  c.per_user_cooldown_ms = (+document.getElementById('r-usercd').value || 0) * 60000;
  c.max_pending = +document.getElementById('r-pending').value || 6;
  c.queue_stale_ms = (+document.getElementById('r-stale').value || 60) * 1000;
  c.speak_question_max_chars = +document.getElementById('r-qmax').value || 0;
  c.max_speak_chars = +document.getElementById('r-speakmax').value || 0;
  c.max_reply_chars = +document.getElementById('r-maxa').value || 60;
  c.priority = +document.getElementById('r-prio').value || 6;
  c.weather = Object.assign({}, c.weather || {}, {
    enabled: document.getElementById('r-weather').value === '1',
    default_city: document.getElementById('r-city').value.trim() || '郑州'
  });
  c.llm = Object.assign({}, c.llm || {}, {
    enabled: document.getElementById('r-llm').value === '1',
    base_url: document.getElementById('r-llmurl').value.trim(),
    model: document.getElementById('r-llmmodel').value.trim()
  });
  c.presets = parsePresets(document.getElementById('r-presets').value);
  c.intros = [0, 1, 2].map(i=>{
    const el = document.getElementById('intro' + i);
    return el ? el.value.trim() : '';
  });
  c.fallback = document.getElementById('r-fallback').value.split('\n')
    .map(x=>x.trim()).filter(Boolean);
  const r = await api('/api/replies', c);
  const key = document.getElementById('r-llmkey').value.trim();
  let kmsg = '';
  if(key){
    const rk = await api('/api/llm-key', {key: key});
    kmsg = rk.ok ? ('，Key ' + rk.msg) : ('，Key 保存失败：' + (rk.msg||''));
    if(rk.ok) document.getElementById('r-key-state').textContent = '（已配置 Key）';
    document.getElementById('r-llmkey').value = '';
  }
  document.getElementById('r-save-msg').textContent = r.ok
    ? ('已保存 ' + new Date().toLocaleTimeString() + '（预设 ' + c.presets.length + ' 条' + kmsg + '）')
    : ('失败: ' + (r.msg||''));
}

async function sayIntro(){
  const sel = document.querySelector('input[name="intro"]:checked');
  const idx = sel ? +sel.value : 0;
  const ta = document.getElementById('intro' + idx);
  const text = (ta.value || '').trim();
  const msg = document.getElementById('intro-msg');
  const btn = document.getElementById('intro-say');
  if(!text){
    msg.textContent = '这段还是空的，先写一句';
    msg.style.color = '#c0392b';
    return;
  }
  btn.disabled = true;
  msg.textContent = '发送中…';
  msg.style.color = '';
  const r = await api('/api/intro-say', {text: text});
  btn.disabled = false;
  msg.textContent = r.ok ? ('已让它说第 ' + (idx + 1) + ' 段') : ('失败：' + (r.msg || '未知原因'));
  msg.style.color = r.ok ? '#2ea043' : '#c0392b';
  setTimeout(()=>{ msg.textContent = ''; }, 5000);
}

async function testReply(){
  const r = await api('/api/reply-test', {text: document.getElementById('r-test').value});
  const el = document.getElementById('r-out');
  if(!r.ok){ el.innerHTML = '<span style="color:#c0392b">' + esc(r.msg||'失败') + '</span>'; return; }
  if(!r.has_marker){ el.innerHTML = '<span style="color:#b45309">' + esc(r.msg) + '</span>'; return; }
  el.innerHTML = '<div style="padding:10px;background:#f6f8fa;border-radius:8px">「' + esc(r.speak) + '」</div>'
    + '<div class="hint">问题：' + esc(r.question||'（空）') + ' · 来源：' + esc(r.source)
    + ' · 限流：' + esc(r.limit) + '</div>';
}

// ---------------------------------------------------------------- 知识库 / 高频问题
let KB = [];

async function loadKb(){
  const c = await api('/api/knowledge');
  KB = c.entries || [];
  document.getElementById('kb-msg').textContent =
    '共 ' + KB.length + ' 段，来源 knowledge.md · ' + new Date().toLocaleTimeString();
  renderKb();
  await loadKbExperience();
}

function renderKb(){
  const box = document.getElementById('kb-list');
  if(!box) return;
  const q = (document.getElementById('kb-search').value || '').trim().toLowerCase();
  box.innerHTML = '';
  let shown = 0;
  KB.forEach(e=>{
    const hay = ((e.title||'') + ' ' + (e.keywords||[]).join(' ') + ' '
                 + (e.body||'')).toLowerCase();
    if(q && hay.indexOf(q) < 0) return;
    shown++;
    const d = document.createElement('details');
    d.className = 'kbitem';
    const s = document.createElement('summary');
    s.innerHTML = '<b>' + esc(e.title || '（未命名）') + '</b> '
      + '<span class="tag">' + esc(e.type || 'fact') + '</span> '
      + (e.source === 'adopted' ? '<span class="muted">来自高频问题采纳</span> ' : '')
      + '<span class="muted">' + esc((e.keywords||[]).join(' / ')) + '</span>';
    d.appendChild(s);
    const body = document.createElement('div');
    body.className = 'kbody';
    body.innerHTML = '<div style="white-space:pre-wrap;line-height:1.8">'
      + esc(e.body || '') + '</div>';
    d.appendChild(body);
    box.appendChild(d);
  });
  if(!shown){
    box.innerHTML = '<div class="hint">'
      + (q ? '没有匹配的条目' : '还没有知识，编辑 knowledge.md 后点「重新加载」。') + '</div>';
  }
}

async function loadKbExperience(){
  const r = await api('/api/experience');
  const msg = document.getElementById('kb-learn-msg');
  const box = document.getElementById('kb-suggest');
  if(!r || r.ok === false){
    box.innerHTML = '<div class="hint">' + esc((r && r.msg) || '经验库还没准备好') + '</div>';
    return;
  }
  const st = r.stats || {}, cfg = r.settings || {};
  document.getElementById('kb-learn').value = cfg.enabled === false ? '0' : '1';
  document.getElementById('kb-min').value = cfg.min_count || 3;
  document.getElementById('kb-thr').value = cfg.merge_threshold || 0.72;
  document.getElementById('kb-cd').value = Math.round((cfg.suggest_cooldown_s || 1800) / 60);
  msg.textContent = (r.offline
      ? '采集未运行：以下是本地记录，采纳会直接写入，下次启动生效'
      : '采集运行中：采纳后立即生效')
    + ' · 共记录 ' + (st.asks || 0) + ' 次提问 / ' + (st.clusters || 0)
    + ' 个问题 / 待审 ' + (st.pending || 0) + ' 条';
  box.innerHTML = '';
  const list = (r.clusters || []).filter(c => c.status === 'suggested' || c.status === 'open');
  if(!list.length){
    box.innerHTML = '<div class="hint">还没有需要处理的问题。观众把同一个问题问够次数后，这里会出现建议。</div>';
    return;
  }
  list.slice(0, 30).forEach(c=>{
    const div = document.createElement('div');
    div.className = 'kbsug';
    div.innerHTML =
      '<div><b>' + esc(c.canonical || '') + '</b> <span class="muted">累计 '
      + (c.count || 0) + ' 次 · ' + (c.status === 'suggested' ? '待审' : '待生成')
      + '</span></div>'
      + '<div class="muted" style="font-size:12px;margin-top:4px">变体：'
      + esc((c.samples || []).join(' / ')) + '</div>'
      + '<textarea class="sug-text"></textarea>'
      + '<div class="row">'
      + '<div style="flex:0 0 auto"><button class="primary">采纳</button></div>'
      + '<div style="flex:0 0 auto"><button class="regen">重新生成</button></div>'
      + '<div style="flex:0 0 auto"><button class="dismiss">忽略</button></div>'
      + '<div style="flex:1"><span class="muted sug-msg"></span></div>'
      + '</div>';
    const ta = div.querySelector('.sug-text');
    ta.value = (c.suggestion && c.suggestion.text) || '';
    ta.placeholder = '还没有建议；点「重新生成」让模型写一版，或直接在这里写';
    const out = div.querySelector('.sug-msg');
    div.querySelector('button.primary').onclick = async ()=>{
      const text = (ta.value || '').trim();
      if(!text){ out.textContent = '先写一句或点重新生成'; return; }
      const rr = await api('/api/experience/accept', {id: c.id, text: text});
      out.textContent = rr.msg || (rr.ok ? '已采纳' : '失败');
      if(rr.ok) setTimeout(loadKb, 400);
    };
    div.querySelector('button.regen').onclick = async ()=>{
      const rr = await api('/api/experience/regenerate', {id: c.id});
      out.textContent = rr.msg || '';
      if(rr.ok) setTimeout(loadKbExperience, 6000);
    };
    div.querySelector('button.dismiss').onclick = async ()=>{
      const rr = await api('/api/experience/dismiss', {id: c.id});
      out.textContent = rr.ok ? '已忽略' : (rr.msg || '失败');
      if(rr.ok) setTimeout(loadKbExperience, 400);
    };
    box.appendChild(div);
  });
}

async function saveKbSettings(){
  const body = {
    enabled: document.getElementById('kb-learn').value === '1',
    min_count: +document.getElementById('kb-min').value || 3,
    merge_threshold: +document.getElementById('kb-thr').value || 0.72,
    suggest_cooldown_s: Math.max(60, Math.round((+document.getElementById('kb-cd').value || 30) * 60))
  };
  const r = await api('/api/experience/settings', body);
  document.getElementById('kb-learn-msg').textContent =
    r.ok ? '设置已保存' : ('保存失败：' + (r.msg || ''));
  if(r.ok) loadKbExperience();
}

function esc(s){ return String(s==null?'':s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

async function loadCookie(){
  const el = document.getElementById('ck-state');
  if(!el) return;
  const s = await api('/api/cookie');
  if(!s.ok){ el.textContent = '（' + (s.msg || '读取失败') + '）'; return; }
  el.textContent = '（' + s.fields + ' 个字段' + (s.has_session ? '，含 sessionid' : '，缺 sessionid')
    + '，更新于 ' + s.updated + '）';
  el.style.color = s.has_session ? '' : '#c0392b';
}

async function saveCookie(){
  const raw = document.getElementById('ck-raw').value.trim();
  const msg = document.getElementById('ck-msg');
  const btn = document.getElementById('ck-save');
  if(!raw){ msg.textContent = '先把 Cookie 粘进来'; msg.style.color = '#c0392b'; return; }
  btn.disabled = true;
  msg.textContent = '校验中，可能要 5 秒…';
  msg.style.color = '';
  const r = await api('/api/cookie', {raw: raw});
  btn.disabled = false;
  msg.textContent = (r.ok ? '✓ ' : '✗ ') + (r.msg || '');
  msg.style.color = r.ok ? '#2ea043' : '#c0392b';
  if(r.ok){
    document.getElementById('ck-raw').value = '';
    loadCookie();
    refresh();
  }
}

const ckBtn = document.getElementById('ck-save');
if(ckBtn) ckBtn.onclick = saveCookie;
loadCookie();

const ckAutoBtn = document.getElementById('ck-auto');
if(ckAutoBtn) ckAutoBtn.onclick = ()=> autoCookie(false);
const ckFreshBtn = document.getElementById('ck-fresh');
if(ckFreshBtn) ckFreshBtn.onclick = ()=> autoCookie(true);

async function autoCookie(fresh){
  const msg = document.getElementById('ck-auto-msg');
  if(fresh && !confirm('「换号登录」会先退出这个窗口里的抖音账号，然后请你扫码登录新号。\n'
      + '已保存的配置不会被破坏，登录成功后才覆盖。继续？')){ return; }
  msg.style.color = '#b26b00';
  msg.textContent = fresh ? '正在打开窗口（会先退出当前账号）…' : '正在打开浏览器窗口…';
  const r = await api('/api/cookie-auto', {go: 1, fresh: !!fresh});
  msg.textContent = r.msg || '';
  if(!r.ok){
    msg.style.color = '#c62828';
    alert(r.msg || '打不开浏览器');
  }
  refresh();
}

let AUTO_DONE_TS = 0;
function renderAuto(a){
  const b1 = document.getElementById('ck-auto');
  const b2 = document.getElementById('ck-fresh');
  const msg = document.getElementById('ck-auto-msg');
  if(!msg || !a) return;
  if(a.running){
    if(b1) b1.disabled = true;
    if(b2) b2.disabled = true;
    msg.style.color = '#b26b00';
    msg.textContent = a.msg || '等待扫码…';
    return;
  }
  if(b1) b1.disabled = false;
  if(b2) b2.disabled = false;
  if(a.step === 'done'){ msg.style.color = '#1a7f37'; }
  else if(a.ok === false){ msg.style.color = '#c62828'; }
  else { msg.style.color = ''; }
  if(a.msg) msg.textContent = a.msg;
  if(a.step === 'done' && a.web_rid && a.ts !== AUTO_DONE_TS){
    AUTO_DONE_TS = a.ts;
    const el = document.getElementById('room');
    if(el && el.value !== a.web_rid) el.value = a.web_rid;
  }
}

async function detectRoom(){
  const btn = document.getElementById('room-auto');
  const msg = document.getElementById('room-msg');
  btn.disabled = true;
  msg.style.color = '#b26b00';
  msg.textContent = '正在用你的账号查直播间（要开个后台浏览器，约 10 秒）…';
  const r = await api('/api/detect-room');
  btn.disabled = false;
  if(r.ok){
    document.getElementById('room').value = r.web_rid;
    msg.style.color = '#1a7f37';
    msg.textContent = '已填好 ' + r.web_rid
      + (r.nick ? ('（' + r.nick + ' 正在直播）') : '') + '，点「开始检测」就能接上';
  } else {
    msg.style.color = '#c62828';
    msg.textContent = r.msg || '没找到直播间号';
  }
}

const introBtn = document.getElementById('intro-say');
if(introBtn) introBtn.onclick = sayIntro;

refresh(); setInterval(refresh, 2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200, ctype='application/json'):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        # 页面改动后必须立即生效，否则会看到缓存的旧界面
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            return self._send(PAGE.encode(), ctype='text/html')
        if self.path == '/api/state':
            with LOCK:
                log = list(LOG)[-300:]
                counts = dict(COUNTS)
                chats = list(CHATS)[-30:]
            return self._send({
                'running': RUNNER.running(),
                'room': RUNNER.room or load_state().get('room', ''),
                'counts': counts, 'log': log, 'boot': BOOT,
                'who': whoami(), 'chats': chats, 'auto': auto_state(),
            })
        if self.path == '/api/gifts':
            return self._send(load_gifts())
        if self.path == '/api/interactions':
            return self._send(load_interactions())
        if self.path == '/api/cookie':
            return self._send(cookie_status())
        if self.path == '/api/cookie-auto':
            return self._send(auto_state())
        if self.path == '/api/detect-room':
            return self._send(detect_room())
        if self.path == '/api/replies':
            c = load_replies()
            if isinstance(c, dict):
                kp = os.path.join(HERE, 'secrets', 'llm_key')
                c['has_llm_key'] = os.path.isfile(kp) and os.path.getsize(kp) > 2
            return self._send(c)
        if self.path == '/api/knowledge':
            return self._send(load_knowledge())
        if self.path == '/api/experience':
            r = admin_call('/api/experience', {})
            if not r.get('ok') and ('ok' not in r or '管线没响应' in (r.get('msg') or '')):
                r = local_experience()       # 采集没跑：读本地文件，标注 offline
            return self._send(r)
        self._send({'ok': False, 'msg': 'not found'}, 404)

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            body = {}

        if self.path == '/api/start':
            room = body.get('room', '').strip()
            if not room:
                return self._send({'ok': False, 'msg': '直播间号为空'})
            # 采集服务没起就自动拉起来，避免"点了开始却没反应"
            okb, msgb = ensure_backend()
            if not okb:
                return self._send({'ok': False, 'msg': msgb})
            ok, msg = RUNNER.start(room)
            if ok:
                st = load_state()
                st['room'] = room
                save_state(st)
            return self._send({'ok': ok, 'msg': msg + '（' + msgb + '）'})

        if self.path == '/api/stop':
            return self._send({'ok': RUNNER.stop()})

        if self.path == '/api/gifts':
            cfg = body or {}
            cfg.setdefault('version', 1)
            # 至少保留一个档位：否则没设专属话术的礼物会退化成通用兜底句
            if not cfg.get('tiers'):
                return self._send({'ok': False,
                                   'msg': '至少要保留一个钻石档位，否则普通礼物没有话术可用'})
            try:
                write_json(GIFTS, cfg)
                return self._send({'ok': True})
            except Exception as e:
                return self._send({'ok': False, 'msg': str(e)})

        if self.path == '/api/interactions':
            cur = load_interactions()
            body = body or {}
            for k in ('like', 'follow'):
                if isinstance(body.get(k), dict):
                    merged = dict(cur.get(k) or {})
                    merged.update(body[k])       # 只覆盖界面上有的字段，priority 等保持
                    if not [t for t in (merged.get('thanks') or []) if t]:
                        return self._send({'ok': False,
                                           'msg': '「%s」至少要留一句，否则收到时没话说'
                                                  % ('点赞' if k == 'like' else '关注')})
                    cur[k] = merged
            from interactions import _save
            try:
                _save(INTERACTIONS, cur)
                return self._send({'ok': True})
            except Exception as e:
                return self._send({'ok': False, 'msg': str(e)})

        if self.path == '/api/llm-key':
            key = (body.get('key') or '').strip()
            if len(key) < 8:
                return self._send({'ok': False, 'msg': 'Key 太短，像是没填对'})
            try:
                d = os.path.join(HERE, 'secrets')
                os.makedirs(d, exist_ok=True)
                p = os.path.join(d, 'llm_key')
                with open(p, 'w', encoding='utf-8') as f:
                    f.write(key + '\n')
                try:
                    os.chmod(p, 0o600)
                except OSError:
                    pass
                # 填了 Key 就默认把模型打开，不然用户会以为没生效
                turned_on = False
                try:
                    cfg = load_replies()
                    cfg.pop('has_llm_key', None)
                    llm = cfg.setdefault('llm', {})
                    if not llm.get('enabled'):
                        llm['enabled'] = True
                        turned_on = True
                    llm.setdefault('base_url', 'https://api.deepseek.com/v1')
                    llm.setdefault('model', 'deepseek-chat')
                    write_json(REPLIES, cfg)
                except Exception:
                    pass
                # 只回长度，不回显明文
                return self._send({'ok': True, 'msg': '已保存 %d 位%s'
                                   % (len(key), '，模型已自动开启' if turned_on else '')})
            except Exception as e:
                return self._send({'ok': False, 'msg': str(e)})

        if self.path == '/api/replies':
            cfg = body or {}
            cfg.pop('has_llm_key', None)   # 只读字段，别写进配置
            if not (cfg.get('marker') or '').strip():
                return self._send({'ok': False, 'msg': '唤醒标记不能为空'})
            try:
                write_json(REPLIES, cfg)
                return self._send({'ok': True})
            except Exception as e:
                return self._send({'ok': False, 'msg': str(e)})

        if self.path in ('/api/experience/accept', '/api/experience/dismiss',
                         '/api/experience/regenerate', '/api/experience/settings'):
            try:
                return self._send(experience_call(self.path, body or {}))
            except Exception as e:
                return self._send({'ok': False, 'msg': str(e)})

        if self.path == '/api/reply-test':
            # 只算不说：构造一个临时引擎，不碰限流状态
            try:
                from reply_engine import ReplyEngine
                eng = ReplyEngine(REPLIES)
                text = (body.get('text') or '').strip()
                has, q = eng.parse(text)
                if not has:
                    return self._send({'ok': True, 'has_marker': False,
                                       'msg': '没有唤醒标记「%s」，这条不会回复' % eng.marker})
                ans, src = eng.answer(q, 'test')
                speak = eng.compose({'name': '测试用户', 'question': q, 'text': ans})
                return self._send({'ok': True, 'has_marker': True, 'question': q,
                                   'source': src, 'answer': ans,
                                   'speak': speak,
                                   'limit': '同一人 %.0f 秒 / 队列 %.0f 秒作废'
                                            % (eng.user_cd / 1000, eng.stale_ms / 1000)})
            except Exception as e:
                return self._send({'ok': False, 'msg': '%s: %s' % (type(e).__name__, e)})

        if self.path == '/api/preview':
            try:
                return self._send(preview(body))
            except Exception as e:
                return self._send({'text': f'渲染失败: {e}', 'tier': '-', 'priority': 0})

        if self.path == '/api/reply-danmaku':
            # 页面点选一条弹幕 → 交给 pipeline 生成回答并播报
            name = (body.get('name') or '').strip()
            text = (body.get('text') or '').strip()
            if not text:
                return self._send({'ok': False, 'msg': '这条弹幕没有内容'})
            if not RUNNER.running():
                return self._send({'ok': False, 'msg': '采集没在跑，先点开始'})
            return self._send(admin_call('/api/reply-danmaku',
                                         {'name': name, 'text': text}))

        if self.path == '/api/cookie':
            ok, msg = save_cookie(body.get('raw') or '')
            if not ok:
                return self._send({'ok': False, 'msg': msg})
            was_running = RUNNER.running()
            room = RUNNER.room or (load_state().get('room') or '')
            if was_running:
                RUNNER.stop()
                time.sleep(0.6)
            okb, msgb = restart_backend()
            resumed = False
            if was_running and okb and room:
                resumed, _ = RUNNER.start(room)
            time.sleep(0.8)
            return self._send({'ok': okb, 'msg': msg + '；' + msgb,
                               'status': cookie_status(), 'resumed': resumed})

        if self.path == '/api/cookie-auto':
            ok, msg = auto_login_start(bool((body or {}).get('fresh')))
            return self._send({'ok': ok, 'msg': msg, 'auto': auto_state()})

        if self.path == '/api/detect-room':
            return self._send(detect_room())

        if self.path == '/api/intro-say':
            # 主播主动让机器人做自我介绍（选中的那段），走管线队列、不打断当前播报
            text = (body.get('text') or '').strip()
            if not text:
                return self._send({'ok': False, 'msg': '这段是空的，先写一句'})
            if not RUNNER.running():
                return self._send({'ok': False, 'msg': '采集没在跑，先点开始'})
            return self._send(admin_call('/api/say',
                                         {'text': text, 'priority': 8,
                                          'interrupt': False}))

        if self.path == '/api/say':
            # 从页面直接让机器人说一句，用来验证隧道与 TTS 是否通
            text = (body.get('text') or '').strip()
            if not text:
                return self._send({'ok': False, 'msg': '文本为空'})
            try:
                import speaker as spk
                sp = spk.Speaker()
                try:
                    r = sp.speak(text, priority=int(body.get('priority') or 7))
                finally:
                    sp.close()
                return self._send({'ok': bool(r.get('accepted')), 'result': r})
            except Exception as e:
                return self._send({'ok': False, 'msg': str(e)[:300]})

        if self.path == '/api/simulate':
            # 构造与真实采集完全一致的事件结构，走同一套 rules + speaker，
            # 不写模拟专用分支 —— 这里通了，真实礼物一定通
            kind = body.get('kind') or 'gift'
            user = (body.get('user') or '测试用户').strip()[:20]
            now = int(time.time() * 1000)
            import uuid as _uuid
            if kind == 'chat':
                text = body.get('text') or '测试弹幕'
                with LOCK:
                    LOG.append(f'[{time.strftime("%H:%M:%S")}] 弹幕 {user}：{text}  [模拟]')
                    COUNTS['chat'] += 1
                return self._send({'ok': True, 'msg': '弹幕已模拟（当前版本不自动回复弹幕）'})

            cfg = load_gifts()
            gift = (body.get('gift') or '小心心').strip()
            count = max(int(body.get('count') or 1), 1)
            diamond = int(body.get('diamond') or
                          (cfg.get('gift_prices') or {}).get(gift, 1))
            ev = {'ts': now, 'type': 'gift', 'user_id': 'test_001',
                  'user_name': user, 'gift': gift, 'gift_id': 'sim',
                  'diamond': diamond, 'repeat': count, 'combo': count, 'group': 0,
                  'msg_id': 'sim-' + _uuid.uuid4().hex[:10]}

            import rules as R
            import speaker as S
            gr = R.GiftRules(GIFTS)
            tasks = list(gr.feed(ev, now))
            tasks += list(gr.tick(now + 100000))   # 立即结算，不等连击窗口
            if not tasks:
                return self._send({'ok': False, 'msg': '规则未产出任务（可能命中不回答清单）'})
            task = tasks[0]
            with LOCK:
                LOG.append(f'[{time.strftime("%H:%M:%S")}] 收到礼物 {user} / {gift} x{count}  [模拟]')
                COUNTS['gift'] += 1
            sp = S.Speaker()
            try:
                r = sp.speak(task['text'], priority=task['priority'],
                             interrupt=task['interrupt'], domain='robot-live-gift',
                             source=f'模拟/{gift}')
            finally:
                sp.close()
            with LOCK:
                if r.get('accepted'):
                    COUNTS['speak'] += 1
                LOG.append(('     [播报OK ] ' if r.get('accepted') else '     [播报失败] ')
                           + f"{r.get('elapsed_ms','?')}ms  p{task['priority']}  {task['text']}")
            return self._send({'ok': bool(r.get('accepted')), 'text': task['text'],
                               'tier': task.get('tier'), 'priority': task['priority'],
                               'elapsed_ms': r.get('elapsed_ms'), 'error': r.get('error')})

        self._send({'ok': False, 'msg': 'not found'}, 404)


def main():
    port = 8780
    for i, a in enumerate(sys.argv):
        if a == '--port' and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    threading.Thread(target=_who_loop, daemon=True).start()
    print(f'控制台: http://127.0.0.1:{port}')
    print('（Ctrl+C 停止）')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        RUNNER.stop()


if __name__ == '__main__':
    main()

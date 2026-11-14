#!/usr/bin/env python
# coding: utf-8
"""环境自检与安装：先看缺什么，再补什么。

检查项：Python 版本 → 必需文件 → 依赖包 → 私钥权限 → 机器人连通 → 端口占用 → 配置齐全
任何一项能自动修的就地修，修不了的给出具体该怎么做。

用法：双击 安装依赖.bat，或 python setup.py
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

PASS, WARN, FAIL = '[OK]', '[!] ', '[X] '
passed, warned, failed = [], [], []


def ok(msg, detail=''):
    passed.append(msg)
    print(PASS, msg, detail)


def warn(msg, detail=''):
    warned.append(msg)
    print(WARN, msg, detail)


def bad(msg, detail=''):
    failed.append(msg)
    print(FAIL, msg, detail)


def check_python():
    v = sys.version_info
    ver = '%d.%d.%d' % (v[0], v[1], v[2])
    if v >= (3, 10):
        ok('Python 版本', ver)
    elif v >= (3, 8):
        warn('Python 版本偏低', ver + '（建议 3.10+，当前版本应该能跑）')
    else:
        bad('Python 版本过低', ver + '（需要 3.8 以上，建议 3.10+）')
    if os.name != 'nt':
        warn('操作系统不是 Windows', '采集器 douyinLive.exe 只能在 Windows 上运行')


def check_files():
    required = [
        'gui.py', 'pipeline.py', 'admin.py', 'backend.py', 'rules.py',
        'reply_engine.py', 'speaker.py', 'weather.py', 'bans.py', 'go_collector.py',
        'knowledge.py', 'experience.py',
        'replies.json', 'gifts.json', 'knowledge.md',
        'bin/douyinLive.exe', 'bin/config.yaml',
    ]
    missing = [f for f in required if not os.path.isfile(os.path.join(HERE, f))]
    if missing:
        bad('程序文件不全', '缺 %d 个：%s' % (len(missing), '、'.join(missing[:5])))
    else:
        ok('程序文件', '%d 个必需文件齐全' % len(required))

    key = os.path.join(HERE, 'secrets', 'qiyuan_t1')
    if os.path.isfile(key):
        ok('机器人私钥', 'secrets/qiyuan_t1')
    else:
        bad('缺机器人私钥', '没有 secrets/qiyuan_t1，无法让机器人说话')

    llm = os.path.join(HERE, 'secrets', 'llm_key')
    if os.path.isfile(llm) and os.path.getsize(llm) > 8:
        ok('大模型 Key', '已配置（%d 位）' % (os.path.getsize(llm) - 1))
    else:
        warn('大模型 Key 未配置', '不影响启动，但弹幕只能答预设和天气')


def check_deps():
    try:
        import websocket
        ok('依赖 websocket-client', getattr(websocket, '__version__', '已安装'))
        return True
    except ImportError:
        pass

    print('     正在安装 websocket-client ...')
    req = os.path.join(HERE, 'requirements.txt')
    sources = [None,
               'https://pypi.tuna.tsinghua.edu.cn/simple',
               'https://mirrors.aliyun.com/pypi/simple']
    for src in sources:
        cmd = [sys.executable, '-m', 'pip', 'install', '-r', req, '--disable-pip-version-check']
        if src:
            cmd += ['-i', src]
            print('     换源重试：%s' % src)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=240)
        except Exception as e:
            print('     pip 执行失败：%s' % e)
            continue
        if r.returncode == 0:
            ok('依赖安装成功', 'websocket-client')
            return True
    bad('依赖安装失败', '检查网络后重试，或手动执行： pip install -r requirements.txt')
    return False


def check_node():
    """一键获取 Cookie / 识别直播间号需要 Node.js；
    没有也不影响主流程，手动粘贴 Cookie 一样能用"""
    node = shutil.which('node')
    has_mod = os.path.isdir(os.path.join(HERE, 'node_modules', 'playwright-core'))
    browser = ''
    for p in (r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
              r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
              r'C:\Program Files\Google\Chrome\Application\chrome.exe',
              r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'):
        if os.path.exists(p):
            browser = os.path.basename(os.path.dirname(os.path.dirname(p)))
            break
    if node and has_mod and browser:
        ok('一键获取 Cookie 可用', 'Node + playwright-core + %s' % browser)
        return
    miss = []
    if not node:
        miss.append('没装 Node.js（https://nodejs.org 下载 LTS）')
    if not has_mod:
        miss.append('缺 node_modules（项目目录执行 npm i playwright-core）')
    if not browser:
        miss.append('没找到 Edge 或 Chrome')
    warn('一键获取 Cookie 用不了', '；'.join(miss)
         + '。可以手动粘贴 Cookie，其它功能不受影响')


def _cookie_alive(raw):
    """用这份 Cookie 问一句抖音：还是登录态吗"""
    import urllib.request
    req = urllib.request.Request(
        'https://live.douyin.com/webcast/user/me/?aid=6383',
        headers={'Cookie': raw,
                 'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                'AppleWebKit/537.36 (KHTML, like Gecko) '
                                'Chrome/131.0.0.0 Safari/537.36'),
                 'Referer': 'https://live.douyin.com/'})
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(req, timeout=6) as r:
            d = json.loads(r.read().decode('utf-8', 'replace'))
        u = (d or {}).get('data') or {}
        return str(u.get('nickname') or '')
    except Exception:
        return ''


def check_cookie():
    """登录态是收礼物的前提：Cookie 过期时采集照常连上，但礼物一条都收不到"""
    cfg = os.path.join(HERE, 'bin', 'config.yaml')
    try:
        with open(cfg, encoding='utf-8') as f:
            txt = f.read()
    except OSError:
        bad('读不到 bin/config.yaml', '采集服务没法启动')
        return
    m = re.search(r'(?m)^\s*douyin:\s*"(.*)"\s*$', txt)
    raw = (m.group(1) if m else '').replace('\\"', '"').replace('\\\\', '\\')
    if 'sessionid=' not in raw:
        bad('抖音 Cookie 缺 sessionid', '控制台 → 直播间 →「用当前账号获取」')
        return
    nick = _cookie_alive(raw)
    if nick:
        ok('抖音登录态有效', '当前账号 %s' % nick)
    else:
        warn('抖音登录态可能已失效',
             '控制台点「用当前账号获取」或「换号登录」；不然采集会连上但收不到礼物')


def fix_key_permission():
    key = os.path.join(HERE, 'secrets', 'qiyuan_t1')
    if os.name != 'nt' or not os.path.isfile(key):
        return
    user = os.environ.get('USERNAME', '')
    try:
        subprocess.run(['icacls', key, '/inheritance:r',
                        '/grant:r', '%s:R' % user, '/grant:r', 'SYSTEM:R'],
                       capture_output=True, timeout=20)
        ok('私钥权限', '已收紧（否则 SSH 会拒绝加载）')
    except Exception:
        warn('私钥权限没能自动收紧', '若播报报 UNPROTECTED PRIVATE KEY FILE，手动跑 icacls')


def load_conf():
    conf = {}
    try:
        with open(os.path.join(HERE, 'robot.conf'), 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                conf[k.strip()] = v.strip()
    except OSError:
        pass
    return conf


def check_robot():
    conf = load_conf()
    host = conf.get('host', '')
    user = conf.get('user', 'run')
    key = conf.get('key', 'secrets/qiyuan_t1')
    if not os.path.isabs(key):
        key = os.path.join(HERE, key)
    if not host:
        warn('robot.conf 里没写机器人地址', '播报会失败')
        return
    if not os.path.isfile(key):
        warn('找不到私钥，跳过连通测试', key)
        return
    ssh = shutil.which('ssh') or r'C:\Windows\System32\OpenSSH\ssh.exe'
    if not os.path.isfile(ssh) and not shutil.which('ssh'):
        warn('系统没有 ssh 命令', 'Windows 设置 → 应用 → 可选功能 → 添加 OpenSSH 客户端')
        return
    cmd = [ssh, '-i', key,
           '-o', 'StrictHostKeyChecking=accept-new',
           '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
           '%s@%s' % (user, host), 'echo ROBOTLIVE_OK']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=15)
        out = (r.stdout or '') + (r.stderr or '')
        if 'ROBOTLIVE_OK' in out:
            ok('机器人连通', '%s@%s' % (user, host))
        else:
            detail = out.strip().splitlines()
            bad('连不上机器人', '%s@%s — %s' % (user, host, detail[-1][:80] if detail else '无返回'))
    except Exception as e:
        bad('连不上机器人', str(e)[:100])


def check_ports():
    busy = []
    for port in (1088, 8780, 8771):
        s = socket.socket()
        s.settimeout(0.3)
        try:
            if s.connect_ex(('127.0.0.1', port)) == 0:
                busy.append(port)
        finally:
            s.close()
    if busy:
        warn('端口已被占用', '%s（可能已有一套在运行，或上次没退干净）'
             % '、'.join(str(p) for p in busy))
    else:
        ok('端口空闲', '1088 / 8780 / 8771')


def check_configs():
    try:
        with open(os.path.join(HERE, 'replies.json'), 'r', encoding='utf-8') as f:
            r = json.load(f)
        ok('问答配置', '唤醒词 %s / 预设 %d 条' % (r.get('marker', '-'), len(r.get('presets') or [])))
    except Exception as e:
        bad('replies.json 读不了', str(e)[:80])

    try:
        with open(os.path.join(HERE, 'gifts.json'), 'r', encoding='utf-8') as f:
            g = json.load(f)
        tiers = g.get('tiers') or []
        if tiers:
            ok('礼物配置', '%d 个档位' % len(tiers))
        else:
            bad('gifts.json 档位为空', '控制台里点保存会补回来，或从 gifts.json.bak 恢复')
    except Exception as e:
        bad('gifts.json 读不了', str(e)[:80])

    # Cookie 只能看有没有，判断不了是否过期
    try:
        with open(os.path.join(HERE, 'bin', 'config.yaml'), 'r', encoding='utf-8') as f:
            txt = f.read()
        line = [l for l in txt.splitlines() if l.strip().startswith('douyin:')]
        if line and len(line[0]) > 60:
            ok('抖音 Cookie', '已填写（过期与否要到直播时才知道）')
        else:
            warn('抖音 Cookie 像是空的', '采集会收不到弹幕和礼物')
    except Exception as e:
        warn('读不到 bin/config.yaml', str(e)[:60])


def main():
    print('=' * 58)
    print(' RobotLive 环境自检与安装')
    print('=' * 58)
    print()

    print('--- 1/7 运行环境 ---')
    check_python()
    print()
    print('--- 2/7 文件完整性 ---')
    check_files()
    print()
    print('--- 3/7 依赖包 ---')
    check_deps()
    check_node()
    print()
    print('--- 4/7 权限与配置 ---')
    fix_key_permission()
    check_configs()
    print()
    print('--- 5/7 机器人连通 ---')
    check_robot()
    print()
    print('--- 6/7 端口 ---')
    check_ports()
    print()
    print('--- 7/7 抖音登录态 ---')
    check_cookie()
    print()

    print('=' * 58)
    print(' 通过 %d 项 / 提醒 %d 项 / 失败 %d 项' % (len(passed), len(warned), len(failed)))
    if failed:
        print()
        print(' 还有问题没解决：')
        for f in failed:
            print('   -', f)
        print()
        print(' 全部修好后再启动。')
    elif warned:
        print()
        print(' 可以启动了。上面的提醒不影响开播，但建议留意。')
        print(' 下一步：双击「启动控制台.bat」')
    else:
        print()
        print(' 一切就绪。下一步：双击「启动控制台.bat」')
    print('=' * 58)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

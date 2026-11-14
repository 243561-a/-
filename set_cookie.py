#!/usr/bin/env python
# coding: utf-8
"""
写入/检查小号 Cookie

用法（两种都行）：
    python set_cookie.py                       # 交互式粘贴
    python set_cookie.py "完整cookie字符串"     # 直接传

做三件事：
  1. 清洗：去掉 "Cookie:" 前缀、换行、首尾引号
  2. 检查必备字段（sessionid 必须有，没有就是没登录）
  3. 写 cookie.txt（权限 600），并打印将注入的字段名（不打印值）

写完后用这条复验礼物消息是否开始下发：
    python diag_methods.py <直播间号> 60
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, 'cookie.txt')

# 登录态的关键字段：sessionid 是身份，没有它等于游客
REQUIRED = ['sessionid']
# 常见但非必需，用来给用户一个"看起来对"的判断
NICE = ['sessionid_ss', 'sid_tt', 'uid_tt', 'passport_csrf_token', 'ttwid', 'msToken']


def clean(raw):
    raw = raw.replace('Cookie:', '').replace('\r', ' ').replace('\n', ' ').strip()
    return raw.strip('"').strip("'").strip()


def extract_cookie(raw):
    """从多种粘贴格式里把 Cookie 抠出来。

    支持：
      1. 纯 Cookie 串          sessionid=xx; ttwid=yy
      2. Cookie: 前缀           Cookie: sessionid=xx; ...
      3. curl 命令（整条）      curl 'https://...' -H 'cookie: sessionid=xx; ...'
    第 3 种最省事：DevTools 里右键请求 → 复制 → 以 cURL 格式复制，整段粘进来。
    """
    raw = raw.replace('\r', ' ').replace('\n', ' ').strip()

    low = raw.lower()
    if 'curl ' in low:
        # 优先找 -H 'cookie: ...' / -H "cookie: ..."
        for pat in (r"-H\s+'[Cc]ookie:\s*([^']*)'", r'-H\s+"[Cc]ookie:\s*([^"]*)"',
                    r"-b\s+'([^']*)'", r'-b\s+"([^"]*)"'):
            m = re.search(pat, raw)
            if m:
                return m.group(1).strip()
        # 有些浏览器给的是 --header 'cookie: ...'
        for pat in (r"--header\s+'[Cc]ookie:\s*([^']*)'",
                    r"--header\s+\"[Cc]ookie:\s*([^\"]*)\""):
            m = re.search(pat, raw)
            if m:
                return m.group(1).strip()
        return ''

    # 纯文本：如果夹着 Cookie: 就取其后
    m = re.search(r'[Cc]ookie:\s*(.+)$', raw)
    if m:
        raw = m.group(1)
    return raw.strip().strip('"').strip("'").strip().rstrip(';')


def parse(raw):
    out = {}
    for seg in raw.split(';'):
        seg = seg.strip()
        if not seg or '=' not in seg:
            continue
        k, v = seg.split('=', 1)
        out[k.strip()] = v.strip()
    return out


def main():
    if len(sys.argv) > 1:
        raw = ' '.join(sys.argv[1:])
    else:
        print('把 Cookie 粘进来，回车结束。支持三种：')
        print('  1) 纯 Cookie 串   2) 带 Cookie: 前缀   3) 整条 curl 命令（最省事）')
        print('-' * 60)
        raw = sys.stdin.readline()

    raw = extract_cookie(raw)
    if not raw:
        print('没从这段内容里找到 Cookie。')
        print('如果粘的是 curl，请确认里面带 -H \'cookie: ...\' 这一项。')
        print('那说明这条请求本身就不带 Cookie —— 换一条域名为 live.douyin.com 的请求再复制。')
        return 2

    kv = parse(raw)
    if not kv:
        print('解析不出任何键值对，这段不像 Cookie。')
        return 2

    missing = [k for k in REQUIRED if k not in kv and k + '_ss' not in kv]
    if missing:
        print(f'缺关键字段 {missing} —— 这多半不是登录态的 Cookie。')
        print(f'实际拿到的字段共 {len(kv)} 个：' + ', '.join(sorted(kv))[:300])
        print('')
        print('常见原因：')
        print('  1) 浏览器当前没登录（右上角还是「登录」按钮）')
        print('  2) 请求不是 live.douyin.com / www.douyin.com 域名的（跨域不带 Cookie）')
        print('  3) 复制的不是「请求标头」里的 Cookie，而是响应里的 set-cookie')
        return 3

    with open(DEST, 'w', encoding='utf-8') as f:
        f.write(raw)
    try:
        os.chmod(DEST, 0o600)
    except Exception:
        pass

    print(f'已写入 {DEST}')
    print(f'  字段共 {len(kv)} 个，登录态关键字段齐全')
    print('  会注入的字段：' + ', '.join(sorted(
        k for k in kv if k not in ('ttwid',))))
    print('  ttwid：' + ('用你带来的' if 'ttwid' in kv else '自动获取'))
    print('  未包含（正常的常见字段）：' + (', '.join(k for k in NICE if k not in kv) or '无'))
    print('')
    print('复验：python diag_methods.py <直播间号> 60')
    print('  看到 WebcastGiftMessage 出现在类型列表里 = 通了')
    return 0


if __name__ == '__main__':
    sys.exit(main())

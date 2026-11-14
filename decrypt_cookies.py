#!/usr/bin/env python
# coding: utf-8
"""
从 Chrome 的 Cookie 库里取出抖音登录 Cookie

为什么不能用浏览器自动化：
  Chrome 136 起，使用「默认配置目录」时禁止开启远程调试（端口和管道都不行），
  所以 Playwright / CDP 一启动就超时。这是设计上的限制，绕不过去。

那就直接解库：
  Chrome 127 起 Cookie 用 v20 前缀 + App-Bound Encryption 加密。
  密钥在 Local State 的 os_crypt.app_bound_encrypted_key 里，外面套了 DPAPI。
  本脚本在同一用户下调用 DPAPI 解出密钥，再用 AES-256-GCM 解每条 Cookie。

安全：只写本地 cookie.txt（600），只打印字段名和长度，不打印值。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHROME = os.path.join(os.environ['LOCALAPPDATA'], 'Google', 'Chrome', 'User Data')
LOCAL_STATE = os.path.join(CHROME, 'Local State')
COOKIES = os.path.join(CHROME, 'Default', 'Network', 'Cookies')
OUT = os.path.join(HERE, 'cookie.txt')


class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wt.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def dpapi_unprotect(data, machine=False):
    """CryptUnprotectData 封装，machine=True 时按本机范围解"""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ent_in, _b1 = _blob(data)
    ent_out = DATA_BLOB()
    flags = 0x4 if machine else 0  # CRYPTPROTECT_LOCAL_MACHINE
    ok = crypt32.CryptUnprotectData(ctypes.byref(ent_in), None, None, None, None,
                                    flags, ctypes.byref(ent_out))
    if not ok:
        return None
    try:
        return ctypes.string_at(ent_out.pbData, ent_out.cbData)
    finally:
        kernel32.LocalFree(ent_out.pbData)


def load_key():
    with open(LOCAL_STATE, encoding='utf-8') as f:
        state = json.load(f)
    os_crypt = state.get('os_crypt', {})

    # 1) 新版：App-Bound Encryption（v20 用这个）
    abe = os_crypt.get('app_bound_encrypted_key')
    if abe:
        raw = base64.b64decode(abe)
        if raw[:4] == b'APPB':
            raw = raw[4:]
        for machine in (True, False):  # ABE 通常先按本机范围包了一层
            key = dpapi_unprotect(raw, machine=machine)
            if key and len(key) == 32:
                print(f'  拿到 App-Bound 密钥（{"本机" if machine else "用户"}范围），32 字节')
                return key
        print('  App-Bound 密钥解不开（Chrome 提升了保护等级）')
        return None

    # 2) 老版：os_crypt.encrypted_key（v10 用）
    enc = os_crypt.get('encrypted_key')
    if enc:
        raw = base64.b64decode(enc)
        if raw[:5] == b'DPAPI':
            raw = raw[5:]
        key = dpapi_unprotect(raw)
        if key and len(key) == 32:
            print('  拿到旧版 DPAPI 密钥，32 字节')
            return key
    return None


def decrypt_value(enc, key, host_key=None):
    """解一条 encrypted_value"""
    if not enc:
        return ''
    if enc[:3] == b'v20' or enc[:3] == b'v10':
        if key is None:
            return None
        body = enc[3:]
        nonce, payload = body[:12], body[12:]
        from Crypto.Cipher import AES
        for aad in ((host_key.encode() if host_key else None), None):
            try:
                return AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(
                    payload[:-16], payload[-16:]).decode('utf-8')
            except Exception:
                continue
        return None
    # 极老的格式：整块 DPAPI
    plain = dpapi_unprotect(enc)
    return plain.decode('utf-8') if plain else None


def main():
    if not os.path.isfile(COOKIES):
        print('找不到 Cookies 文件:', COOKIES)
        return 2

    # 复制一份再读，避免占用原库
    tmp = os.path.join(HERE, '_ck_read.db')
    shutil.copyfile(COOKIES, tmp)

    print('读取密钥：')
    key = load_key()
    if key is None:
        print('\n无法取得解密密钥，这条路走不通。')
        return 3

    db = sqlite3.connect(tmp)
    rows = db.execute(
        "select host_key, name, encrypted_value, path, expires_utc, is_secure, is_httponly "
        "from cookies where host_key like '%douyin%'").fetchall()
    db.close()
    os.remove(tmp)

    print(f'\n抖音相关 Cookie {len(rows)} 条，开始解密：')
    ok, bad = {}, 0
    for host, name, enc, path, exp, sec, http in rows:
        val = decrypt_value(enc, key, host)
        if val is None:
            bad += 1
        elif val:
            ok[name] = val

    print(f'  成功 {len(ok)} 个字段，失败 {bad} 条')

    need = ['sessionid', 'sessionid_ss', 'sid_tt', 'uid_tt', 'ttwid',
            'passport_csrf_token', 'msToken', 'odin_tt', 'sid_guard']
    print('\n关键字段：')
    for n in need:
        print(f'  {n:22s} ' + (f'有  {len(ok[n])} 字符' if n in ok else '无'))

    if 'sessionid' not in ok:
        print('\n没有 sessionid —— 这个配置没登录抖音，或登录已过期。')
        return 4

    order = [n for n in need if n in ok] + [n for n in ok if n not in need]
    parts = [f'{n}={ok[n]}' for n in order]
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('; '.join(parts))
    try:
        os.chmod(OUT, 0o600)
    except Exception:
        pass
    print(f'\n已写入 {OUT}（{len(parts)} 个字段）')
    return 0


if __name__ == '__main__':
    sys.exit(main())

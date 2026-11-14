#!/usr/bin/env python
# coding: utf-8
"""
机器人播报通道（不依赖 Node / 不依赖 WorkBuddy 会话）

原理：机器人只允许本机 127.0.0.1:50091 访问 RPC，所以先用 SSH 做一条本地转发，
      再对本地端口发 HTTP JSON。

    ssh -N -L 127.0.0.1:<本地端口>:127.0.0.1:50091 run@<机器人IP>
    POST /rpc/interface.ProductionTesterSrv/Call
    {"service_name":"/aimdk_5Fmsgs/srv/PlayTts","service_type":"PlayTts","data":"<json字符串>"}

用法：
    python speaker.py "你好，我是远仔"
    python speaker.py "抢答" --priority 8 --interrupt

设计要点：
- 隧道常驻复用（一次建链，后续每次播报只是本地 HTTP，约 100ms）
- 隧道断了自动重建一次，不静默失败
- 只提交一次 PlayTts，不自动重发（重发会导致重复播报）
"""

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_conf():
    """项目内 robot.conf（key=value）用于记录机器人地址，换电脑就不用改代码。"""
    conf = {}
    path = os.path.join(HERE, 'robot.conf')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                conf[k.strip()] = v.strip()
    except OSError:
        pass
    return conf


_CONF = _load_conf()


def _default_key():
    """密钥搜索顺序：环境变量 → robot.conf 指定 → 项目内 secrets/ → ~/.ssh

    优先项目内：某些受限运行环境（沙箱/服务化进程）读不到用户主目录下的
    ~/.ssh，表现为 ssh 报 "Load key ...: Permission denied"，
    而工作区目录是可读的。
    """
    env = os.environ.get('QIYUAN_KEY')
    if env:
        return os.path.normpath(env)
    c = _CONF.get('key')
    if c:
        return c if os.path.isabs(c) else os.path.normpath(os.path.join(HERE, c))
    local = os.path.join(HERE, 'secrets', 'qiyuan_t1')
    if os.path.isfile(local):
        return local
    return os.path.normpath(os.path.expanduser('~/.ssh/qiyuan_t1'))


KEY = _default_key()
HOST = os.environ.get('QIYUAN_HOST') or _CONF.get('host') or '192.168.50.9'
USER = os.environ.get('QIYUAN_USER') or _CONF.get('user') or 'run'
RPC_PATH = '/rpc/interface.ProductionTesterSrv/Call'

# 播报程序就跑在机器人本体时，直接打 127.0.0.1:50091，不需要 SSH 隧道
LOCAL = os.environ.get('ROBOTLIVE_LOCAL') == '1'
LOCAL_PORT = int(os.environ.get('ROBOTLIVE_RPC_PORT', '50091'))

CANONICAL = {
    'GetRobotState', 'GetMcAction', 'GetCurrentInputSource', 'GetNetworkState',
    'GetWifiState', 'GetRobotRuntimeModel', 'GetVolume', 'GetMute', 'PlayMove',
}


def _service_name(service):
    """代理对部分服务用全限定名，其余用原始名 —— 以套件实测为准。"""
    return f'/aimdk_5Fmsgs/srv/{service}' if service in CANONICAL else service


def _ssh_bin():
    return shutil.which('ssh') or r'C:\Windows\System32\OpenSSH\ssh.exe'


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _port_open(port, timeout=0.4):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class Speaker:
    def __init__(self, host=HOST, key=KEY, user=USER):
        self.host, self.key, self.user = host, key, user
        self.proc = None
        self.local_port = None

    # ---------- 隧道 ----------
    def _open_tunnel(self):
        self.close()
        port = _free_port()
        cmd = [
            _ssh_bin(), '-N',
            '-L', f'127.0.0.1:{port}:127.0.0.1:50091',
            '-i', self.key,
            '-o', 'StrictHostKeyChecking=no',
            # 不写 known_hosts：机器人在 NAT/重装后指纹会变，且受限环境写不了文件
            '-o', f'UserKnownHostsFile={os.devnull}',
            '-o', 'LogLevel=ERROR',
            # 有提示就失败，避免脚本里卡在交互上
            '-o', 'BatchMode=yes',
            '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=3',
            '-o', 'ConnectTimeout=8',
            f'{self.user}@{self.host}',
        ]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        deadline = time.time() + 12
        while time.time() < deadline:
            if _port_open(port):
                self.local_port = port
                return port
            if self.proc.poll() is not None:
                err = (self.proc.stderr.read() or b'').decode('utf-8', 'replace')
                raise RuntimeError(f'SSH 隧道建立失败: {err.strip()[:300]}')
            time.sleep(0.15)
        raise RuntimeError('SSH 隧道 12 秒内未就绪')

    def ensure(self):
        if LOCAL:
            if not _port_open(LOCAL_PORT):
                raise RuntimeError(f'本机 RPC 端口 {LOCAL_PORT} 未监听')
            return LOCAL_PORT
        if self.local_port and _port_open(self.local_port):
            return self.local_port
        return self._open_tunnel()

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.local_port = None

    # ---------- RPC ----------
    def rpc(self, service, payload, timeout=10.0):
        body = json.dumps({
            'service_name': _service_name(service),
            'service_type': service,
            'data': json.dumps(payload, ensure_ascii=False),
        }, ensure_ascii=False)

        for attempt in (1, 2):
            port = self.ensure()
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
            try:
                conn.request('POST', RPC_PATH, body=body.encode('utf-8'),
                             headers={'Content-Type': 'application/json',
                                      'Content-Length': str(len(body.encode('utf-8')))})
                resp = conn.getresponse()
                raw = resp.read()
                if resp.status != 200:
                    raise RuntimeError(f'HTTP {resp.status}')
                data = json.loads(raw.decode('utf-8'))
                break
            except (ConnectionError, OSError, http.client.HTTPException) as e:
                conn.close()
                if attempt == 2:
                    raise RuntimeError(f'RPC 失败（隧道已重建过一次）: {e}')
                self.close()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        if data.get('code') != 1000:
            err = RuntimeError(f'RPC 返回错误码 {data.get("code")}: {str(data)[:200]}')
            err.rpc_code = data.get('code')
            raise err
        inner = data.get('data')
        if isinstance(inner, str):
            inner = json.loads(inner)
        return inner or {}

    # ---------- 业务 ----------
    def speak(self, text, priority=6, interrupt=False, domain='robot-live',
              trace_id=None, timeout=6.0, source=''):
        text = (text or '').strip()
        if not text:
            raise ValueError('播报文本为空')
        trace_id = trace_id or str(uuid.uuid4())
        t_start = time.time()
        # 埋点：触发源 + 参数，便于区分"没收到消息"和"机器人调用失败"
        sys.stderr.write(f'[机器人调用] 触发源:{source or "-"} | 优先级:p{priority}'
                         f'{" 打断" if interrupt else ""} | 文本:{text}\n')
        sys.stderr.flush()
        payload = {
            'header': {},
            'tts_req': {
                'text': text,
                'priority_level': {'value': int(priority)},
                'priority_weight': 0,
                'domain': domain,
                'trace_id': trace_id,
                'is_interrupted': bool(interrupt),
            },
        }
        result = None
        for attempt in range(3):
            try:
                result = self.rpc('PlayTts', payload, timeout=timeout)
                break
            except RuntimeError as e:
                # 1007 = 服务未启用（请求被明确拒绝，无副作用），可安全重试
                # 超时/未知回执绝不重发，否则会重复播报
                if getattr(e, 'rpc_code', None) == 1007 and attempt < 2:
                    time.sleep(0.6)
                    continue
                raise
        ok = bool(result.get('tts_resp', {}).get('is_success'))
        err = (result.get('tts_resp', {}).get('error_message') or '').strip()
        cost = int((time.time() - t_start) * 1000)
        sys.stderr.write(f'[机器人调用] {"成功" if ok else "被拒"} | 耗时:{cost}ms | '
                         f'trace:{trace_id[:8]}' + (f' | 错误:{err}' if err else '') + '\n')
        sys.stderr.flush()
        return {'accepted': ok, 'trace_id': trace_id,
                'error': err, 'elapsed_ms': cost,
                'chars': len(text), 'priority': priority, 'interrupt': interrupt}

    def volume(self):
        r = self.rpc('GetVolume', {'header': {}})
        return int(getattr(_obj(r.get('audio_volume')), 'value', 0) or 0)


def _obj(v):
    class _O:
        pass
    if isinstance(v, dict):
        o = _O()
        o.__dict__.update(v)
        return o
    return v


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        print('用法: python speaker.py "要说的话" [--priority 8] [--interrupt]')
        sys.exit(2)
    priority = 6
    if '--priority' in sys.argv:
        priority = int(sys.argv[sys.argv.index('--priority') + 1])
    sp = Speaker()
    t0 = time.time()
    try:
        r = sp.speak(args[0], priority=priority, interrupt='--interrupt' in sys.argv)
        print(json.dumps({**r, 'elapsed_ms': int((time.time() - t0) * 1000)}, ensure_ascii=False))
    finally:
        sp.close()


if __name__ == '__main__':
    main()

# RobotLive · 抖音直播弹幕与礼物反馈

> **要把项目拷到另一台电脑跑？先看 [迁移与注意事项.md](迁移与注意事项.md)。**
> 本文是开发笔记（设计取舍、踩坑记录），内容较细，新手按迁移文档走三步即可。

范围：**只做「采集直播间信息」和「礼物触发机器人反馈」**。推流、开播、OBS/直播伴侣不在本项目内。

链路：

```
抖音直播间 --(collector)--> NDJSON 事件流 --(rules)--> 播报任务 --(speaker)--> 机器人 TTS
```

`speaker.py` 支持两种模式，同一份代码：

- **本机模式**（推荐，程序跑在机器人上）：`ROBOTLIVE_LOCAL=1`，直接打 `127.0.0.1:50091`，无隧道
- **远程模式**（程序跑在电脑上）：默认，自动建 SSH 本地转发到机器人 `50091`

## 部署位置

**长期方案：采集、规则、播报三段全部放机器人本体，电脑只做开发和备份。**
**当前阶段（2026-11-13 起）：先在电脑上把链路调通，机器人上的试点安装已移除。**

机器人本体的实测数据（2026-11-13 验证，供以后迁移参考）：

| 项目 | 机器人本体实测 |
|---|---|
| 运行时 | Python 3.10.12 + pip 26.2.1，`/home/user/robotlive`（47 G 可用，独立数据分区） |
| 依赖 | `--target` 装到 `pylibs/`（机器人**没有 venv 模块**），5 个包全部装成功 |
| JS 引擎 | `mini_racer` 有 `manylinux_2_31_aarch64` 预编译包；系统自带 `node v24.19.0` |
| 采集实跑 | 32 秒 174 条真实事件，RSS **68 MB 平稳不涨**，CPU ≈ 0.08 核 |
| 播报延迟 | 本地直连 **121~366 ms**（电脑走 SSH 隧道是 162~1449 ms） |
| 整机影响 | 可用内存维持 2.37 GB 左右，采集起停前后无差别 |
| 端到端 | 6 笔礼物 → 6 次开口，0 失败，全程在机器人内完成 |

将来要迁回去，按这三步（已实测可行）：

```bash
mkdir -p /home/user/robotlive && cd /home/user/robotlive
python3 -m pip install --target=pylibs -i https://pypi.tuna.tsinghua.edu.cn/simple \
  requests==2.31.0 betterproto==2.0.0b6 websocket-client==1.7.0 PyExecJS==1.5.1 mini_racer==0.12.4
# 再把本目录的 .py + gifts.json 传到 /home/user/robotlive，源码放 src/
PYTHONPATH=/home/user/robotlive/pylibs ROBOTLIVE_LOCAL=1 python3 pipeline.py <直播间号>
```

## 控制台（遥控器）

程序跑在哪儿，都不影响你用电脑或手机来指挥它。`admin.py` 提供一页网页：

| 功能 | 说明 |
|---|---|
| 临时播报 | 输一句话点发送，机器人立刻说（可打断），就是「我突然想说一句」 |
| 不回答清单 | 增删关键词，命中就不答、不道谢，改完立即生效 |
| 暂停自动应答 | 5/15/30 分钟，暂停期间不自动谢礼物，临时播报照常 |
| 实时状态 | 最近一条消息多久前、礼物数、已开口数、最近说了什么 |

启动时自动生成 `admin-token.txt`（600 权限），页面地址：

```bash
python pipeline.py <直播间号> --admin-port 8770
# → http://<机器IP>:8770/?t=<token>
```

没有 token 一律 403；不想开就加 `--admin-port 0`。控制台断开不影响机器人继续工作，
这是刻意设计：**控制面掉线，数据面不能停。**

## 现状（2026-11-13）

### 已验证

- 采集**不需要自己实现签名**。直接调接口被拦（HTTP 444 / `X-TT-System-Error: 3`），
  但开源引擎 `saermart/DouyinLiveWebFetcher`（2026-08 仍在维护）已把 `a_bogus` 签名、
  wss 连接、Protobuf 解析做完，直接当引擎用。
- 收到 `chat / gift / enter / like / follow / fansclub / stats`，`msg_id` 无重复。
- **JS 引擎**：签名脚本靠 PyExecJS 跑。Windows 上它自动用系统 JScript，所以「不需要 Node」；
  Linux/机器人上 PyExecJS 会找 `node`（机器人自带的 v24.19.0 即可），或可改接 `py_mini_racer`。
  注：`pip install mini_racer` 装出来的模块名是 `py_mini_racer`。
- 播报通道打通：纯 Python，HTTP JSON 直打 RPC，不依赖 Node / 不依赖 WorkBuddy 会话。
  远程模式热状态 160~500 ms（冷启动约 1.3 s，含建隧道）；本机模式 121~366 ms。
- 礼物链路端到端跑通（假事件驱动真播报）：连击 5 次 → 汇总一句「刷了 5 个」；
  脏昵称自动换成「这位朋友」；优先级冲突自动延后补播，6 笔礼物 6 次全部送达。

### 关键突破：礼物消息 = 登录态 + 能被服务端认出的连接参数

**一句话结论（2026-11-13 端到端验证）：光有 Cookie 不够，连接参数写死也会让 Cookie 失效。**
换成 `douyinLive` 服务后，实测收到真实礼物消息：

```
WebcastGiftMessage 2 条
太阳☀️:送给主播 1个人气票
→ user_id 1412210712131128 / 昵称 太阳☀️ / 钻石 1 / msgId 真实
```

根因对照（这是最容易误判的地方）：

| 字段 | 自建栈（`vendor/src`） | douyinLive v2.3.2 |
|---|---|---|
| `first_req_ms` | `1721106114541`（2024-07 写死） | 当前时间戳，每次实时生成 |
| `user_unique_id` | 写死陌生 ID | 按登录态生成 |
| 带 Cookie 后 | **仍然收不到礼物** | **能收到** |

所以「带 Cookie 还是 0 礼物」不是 Cookie 无效，而是被过期参数架空了。
**判定方法**：看连接 URL 里的 `first_req_ms` 是不是当前时间。

以下是排查过程中留下的一次关键实测（未登录态，`diag_methods.py`，75 秒 153 帧）：

**未登录会话，服务端根本不下发 `WebcastGiftMessage`。**

不是解析失败，也不是解析异常被吞。`diag_methods.py` 在 75 秒内收到 153 个数据帧，
把服务端下发的每一种 `msg.method` 全部列出，共 14 种：

```
140 WebcastMemberMessage / 103 WebcastChatMessage / 48 WebcastInRoomBannerMessage
 30 WebcastRoomStatsMessage / 28 WebcastLikeMessage / 25 WebcastRoomUserSeqMessage
 25 WebcastRanklistHourEntranceMessage / 7 WebcastSocialMessage / 7 WebcastFansclubMessage
  2 WebcastResidentGuestMessage / 1 WebcastLowPcuGuideMessage / 1 WebcastChatLikeMessage
  1 WebcastRoomCommentTopicMessage / 1 WebcastRoomRankMessage
解析异常：无
```

`WebcastGiftMessage` 一次都没出现。同级房间 75 秒 103 条弹幕、140 次进场，
不可能零礼物 —— 只能是未登录会话不在礼物推送名单里。

**为什么必须用探针而不能看现象**：上游 `_wsOnMessage` 是

```python
}.get(method)(msg.payload)
except Exception:
    pass
```

未知类型（`.get()` 返回 `None` 再调用）和解析异常都会被静默吞掉，
表现和「没收到」完全一致。只有先把方法名分布打出来，才能排除这一层。

**脱敏不是全局策略，而是按房间变化的**（这一点容易误判）：

| 房间 | 昵称 | user_id |
|---|---|---|
| `38381168921` | 完整真名 | 真实 ID（457 个不同用户） |
| `705704016990` / 多数房间 | `雨***` | 全部 `111111` |

**登录态怎么给**（两条路，现在走第一条）：

1. **`douyinLive` 服务（当前采用）**：Cookie 写进 `bin/config.yaml` 的
   `cookie.douyin`，服务自己处理连接参数。`go_collector.py` 把它的 WS 输出
   归一化成我们的 NDJSON，下游规则引擎零改动。
2. **自建栈**：`collector.py` 会读 `cookie.txt` / `ROBOTLIVE_COOKIE`，把
   `ttwid=` 之外的字段拼进上游的 `f"ttwid={self.ttwid}"`。但**即使这样也收不到
   礼物**，因为参数写死（见上表）。保留它是为了对照和兜底。

**Cookie 怎么来**（Chrome/Edge 137 之后不能直接读库了）：

- 两家都用 App-Bound Encryption（v20），密钥要提权服务才能解，**进程外解不开**；
  临时复制配置目录也不行（密钥跟路径绑定，实测读出来 0 条）。
- Chrome 136 起还禁止在默认配置目录上开远程调试，所以 Playwright 直连现有配置
  会一直超时（我们实测 45 秒超时）。
- **可行做法：受控登录窗口** —— 用全新配置目录起一个浏览器，用户扫码一次，
  Cookie 由浏览器经 CDP 直接交出。脚本见 `login-and-grab.mjs`（`RL_BROWSER=edge`
  可切 Edge），抓到后写 `cookie.txt`（600 权限）。
- 抖音的 `sessionid` 是 HttpOnly，所以 `document.cookie` 拿不到，只能在「网络 →
  请求标头 → Cookie」里复制；有效期约 2 个月（实测到 2027-01-08）。

## GUI 控制台（推荐日常用这个）

### 每次怎么启动（两种，任选）

**① 双击 `启动控制台.bat`**（最省事）
会自动打开浏览器；如果控制台已经在跑，就只打开浏览器，不会重复启动。

**② 命令行**

```bash
cd C:\Users\anstar\Documents\RobotLive
.venv\Scripts\python.exe gui.py          # http://127.0.0.1:8780
.venv\Scripts\python.exe gui.py --port 8888
```

启动后浏览器打开 **http://127.0.0.1:8780** 即可。

**不用手动起采集服务**：在页面上点「开始检测」时，如果 `douyinLive`（1088 端口）
没在跑，页面会自己把它拉起来，返回信息里会显示「采集服务已启动」。
机器人本体也不用在页面上配置，播报走 `secrets/qiyuan_t1` 这条 SSH 隧道。

只监听 `127.0.0.1`，不对外暴露。

### 两个页签

**① 直播间**
- 填房间号或直接粘贴直播间链接（自动提取数字）
- 「开始检测 / 停止」按钮，直接在页面里启停 `pipeline.py`
- **「让机器人说」按钮**：一秒钟验证 SSH 隧道和语音是否通，出问题先按这个
- 实时状态：礼物 / 已开口 / 弹幕 / 进场 计数 + 滚动日志（每 2 秒刷新）

**② 礼物反馈**
- **指定礼物专属话术**（`gift_overrides`）：某个礼物单独设定话术、优先级、是否打断。
  命中后不再走钻石档位。例：玫瑰 → 「祝你今天心情像花一样好」
- **按钻石分档**（`tiers`）：没设专属的礼物走这里，可改名称、钻石区间、优先级、
  是否打断、话术池（一行一句）
- **试听效果**：填礼物名/钻石/昵称，点一下就能看到会说出哪句话、命中哪个档位；
  保存前先确认，不用等真礼物
- 保存写回 `gifts.json`，**200ms 内热加载生效，不用重启**

话术里的占位符：`{user}` 昵称、`{gift}` 礼物名、`{count}` 数量。
昵称会被清洗（去符号、超长截断、含敏感词则换成「这位朋友」）。

### ③ 调试工具（第三个页签）

- **实时消息概览**：弹幕 / 礼物 / 进场 / 已开口 四个计数 + 最近 10 条消息倒序滚动
- **手动模拟发送**：选礼物或弹幕、填昵称数量，点发送就走**真实的 `rules` + `speaker`**
  （不是另写一套模拟分支），机器人会真的开口。用于不刷真礼物也能验规则与播报
- **礼物全字段日志开关**：勾选后打印每条礼物的昵称/UID/礼物ID/单价/数量/总价/连击，
  并标注「普通 · 粉丝团 · 盲盒」，同时打印规则命中结果。对应 `gifts.json` 的
  `debug_gift_full_log`，改完 200ms 热生效，调试完关掉即恢复简洁输出

### 采集自检（启动即检查，不通过就拦住）

`pipeline.py` 与 `go_collector.py` 启动时依次检查并打印：

```
[自检] 采集服务：已连接 127.0.0.1:1088
[自检] 1/3 连接采集服务 ws://127.0.0.1:1088/ws/<房间> ...
[自检] 2/3 直播间状态：已开播 / 未开播
[自检] 3/3 消息接收：正常（xxx ms 内已收到业务消息）
```

失败时**阻塞主流程**并给出排查方向：

| 现象 | 程序输出 | 下一步 |
|---|---|---|
| 1088 无响应 | `[自检][失败] 采集服务未运行` | 起 douyinLive，或用 GUI 点开始（会自动拉起） |
| 未开播 / 房间号错 | `[自检][失败] 直播间状态：未开播` | 确认已开播；私密开播把测试小号加进可见名单 |
| 3 秒无业务消息 | `[自检][告警] 3 秒内没有业务消息` | 冷门房间属正常；持续如此查 Cookie |
| 采集线程中途退出 | `[自检][失败] 采集线程已退出，主流程结束` | 看上方 `[自检]` 日志定位 |

### 播报埋点

每次调 TTS 都会打印两行（走 stderr，不影响事件流）：

```
[机器人调用] 触发源:礼物/玫瑰 | 优先级:p9 打断 | 文本:小红的玫瑰我收到啦...
[机器人调用] 成功 | 耗时:1479ms | trace:86e61cb3
```

失败则打印 `[机器人调用] 被拒 | 耗时:xxxms | trace:... | 错误:...`，
配合 `[播报失败]` 一行能立刻区分「采集端没收到」和「机器人调用失败」。


### 一个礼物配多种说法（随机不重样）

「礼物反馈」页签里，每个指定礼物下面是一个说法列表：

```
小心心          [3 种说法 · 收到时随机选一句]
 1 [谢谢{user}送的小心心！这一颗我收好了。]  ×
 2 [哟，{user}的心意收到啦，比个心给你！]    ×
 3 [{user}的小心心我看见了，谢啦，别破费呀！] ×
 [+ 再加一种说法]  [随机试听]
```

- 每行是一种说法，`+ 再加一种说法` 可加任意多条，`×` 删掉那一条（至少留一条）
- 收到礼物时**随机抽一句**，并且会排除「上一句刚说过的」，所以三种说法会真正轮着来，
  14 条连测 **0 次连续重复**
- 「随机试听」抽一句并让机器人念出来，用来试语气，不用真刷礼物
- 保存即生效（200ms 热加载），不用重启

内部刻度：`rules.py` 的 `_last_pick` 按「礼物名」分别记上次抽中的模板，
`_render(pool_key='gift:小心心')` 时从池里排除它再随机；钻石档位和连击话术各自独立。


### 点赞与关注（每人只谢一次）

「礼物反馈」页签最上面是**点赞与关注**卡片：

```
点赞  [开启▾]   关注 [开启▾]   点赞最小间隔（秒）[2]      [保存]

点赞时念（每行一句，{user} 是昵称）    关注时念
┌──────────────────────────┐    ┌──────────────────────────┐
│ {user}点亮了小心心，谢谢你…  │    │ {user}关注我了！以后就是…   │
│ 谢谢{user}的赞，我这就精神了…│    │ 谢谢{user}的关注，我这就…   │
└──────────────────────────┘    └──────────────────────────┘
```

几个刻意的设计：

- **每个人只谢一次**。抖音点赞会反复推（一个人连点几十下），关注也可能重发。
  同一个 uid 第二次起完全静默——实测连点 100 下只谢 1 句。
- **点赞之间有最小间隔**（默认 2 秒）。一批新人同时点赞时，间隔内的直接被丢掉，
  日志写 `[点赞] 太密集，这次先跳过 某某`。宁可少谢一个，也不能把礼物道谢挤到后面去。
- **关注不受这个间隔限制**，谁关注都谢，但同一个人只谢一次。
- 昵称带禁用词（`傻`/`滚` 之类）会换成「这位朋友」，整句照样说，不会静默丢掉。
- 话术同样会排除上一句，避免连着两遍一样。

内部刻度：`interactions.py` 的 `_seen['like']` / `_seen['follow']` 存已谢过的 uid，
配置在 `interactions.json`，200ms 热加载，改完保存就生效。

> 重启控制台后「已谢过的人」会清空（存在内存里），直播中途重启的话，
> 之前点过赞的人再点一下会再被谢一次。影响很小，但先说清楚。



## 跑起来需要什么（不依赖 WorkBuddy）

**运行时完全不需要 WorkBuddy**，它只在开发改代码时用。

三种启动方式，任选一个：

| 方式 | 命令 | 适合 |
|---|---|---|
| 双击启动（推荐） | `启动直播.bat` | 日常直播，无界面 |
| 带控制台 | `启动控制台.bat` | 边跑边调话术 |
| 命令行 | `.venv\Scripts\python.exe pipeline.py auto --source go` | 开机自启 / 出错排查 |

- `auto` 表示用 `gui-state.json` 里上次的直播间；也可以直接写房间号
- 采集服务（`bin/douyinLive.exe`）会**自动拉起**，不用单独开
- 关掉那个黑窗口 = 停止；不关就一直跑

> ⚠️ 同一时间只能开**一个** pipeline，开两个会导致机器人把同一句话念两遍。

## 弹幕回复（只回带唤醒标记的）

观众在直播间发弹幕，**只有带 `#元仔` 的才会被念出来并回答**，其余只统计：

```
观众：主播多大了            → 不回复（无标记）
观众：#元仔 你多大了        → 小红问，我今年才刚满一岁，正是最能折腾的年纪。
```

全角 `＃`、以及 `# 元仔`（中间带空格）都能识别。

### 回答从哪来（按顺序）

| 顺序 | 来源 | 说明 |
|---|---|---|
| 1 | `replies.json` 的 `presets` | 关键词命中就直接答，零延迟零成本。一行一条：`多大,几岁 \| 回答` |
| 2 | 大模型 API | 可选，OpenAI 兼容接口。`llm.enabled=true` + 环境变量给 key 才生效 |
| 3 | 兜底话术 | 随机一句，保证不冷场 |

模型没配也能跑，只是回答会限定在预设库范围内。

### 开启模型（可选）

```bash
# 任意 OpenAI 兼容服务都行，改 base_url / model 即可
set ROBOTLIVE_LLM_KEY=sk-xxxx
```
```json
"llm": { "enabled": true, "base_url": "https://api.deepseek.com/v1",
         "model": "deepseek-chat", "api_key_env": "ROBOTLIVE_LLM_KEY" }
```

系统提示词里写死了人设（戏精损友型）和红线：不复述观众原话、碰到政治/色情/暴力/
医疗/投资就绕开。输出还会过一遍长度截断和敏感词。

### 串行 + 冷却（直播场景的关键）

回复**不是并发的**，是排队一条条来：

- **同一人 5 分钟只回一次**（`per_user_cooldown_ms = 300000`）
- **串行播报**：一个人问完、机器人把这句话说完，才轮到下一个人的问题
- **队列上限 6 条**：再多人问就丢弃并记日志，避免越堆越多
- **排队超过 60 秒作废**：三分钟前的问题答了反而尴尬
- 优先级 `priority=6`、`interrupt=false`：礼物播报永远优先，问答只排队

实测（3 个人同时喊）：发送时刻 `+0.0s / +2.0s / +3.9s`，顺序与入队一致，互不重叠。

### 在页面上配

「弹幕回复」页签：启用开关、唤醒标记、念之前加的话、两个限流参数、
预设问答（一行 `关键词1,关键词2 | 回答`）、兜底话术、以及「试算」框（只算不说，不动机器人）。

改完保存，200ms 热加载，不用重启。

## 运行

```bash
# 装依赖（虚拟环境已建在 .venv）
./.venv/Scripts/python.exe -m pip install -r vendor/src/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 0) 先起采集服务（必须，礼物只能从这里来；Cookie 在 bin/config.yaml）
cd bin && ./douyinLive.exe -config config.yaml -port 1088 -log-level debug
#    健康检查：curl --noproxy '*' http://127.0.0.1:1088/health

# 1) 只采集，观察事件流（go = 走 douyinLive，能收礼物）
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe go_collector.py <直播间号> 60

# 2) 礼物全链路：采集 -> 规则 -> 机器人开口（默认 --source go）
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe pipeline.py <直播间号>
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe pipeline.py <直播间号> --seconds 300
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe pipeline.py <直播间号> --dry-run     # 不真播，只看话术
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe pipeline.py <直播间号> --source py # 切回自建栈对照
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe pipeline.py fake --replay replay-sample.json  # 假事件自测

# 3) 验靶：跑 N 秒出判决表（判断某个房间/登录态通不通）
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe diag_methods.py <直播间号> 60

# 4) 单独让机器人说话（含隧道自检）
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe speaker.py "测试一句" --priority 8 --interrupt
```

## 明天从这里继续

机器人今天没连上，所以「收到真礼物 → 机器人开口」这最后一步还没跑。
按顺序做三件事即可：

1. 机器人开机、确认在同一网段：`ping 192.168.50.9`
2. 起采集服务（上面第 0 步），再跑
   `pipeline.py <直播间号> --seconds 120`（**不要加 `--dry-run`**）
3. 用另一个号在直播间送 1 个人气票，观察终端里的
   `[播报OK] xxx ms` 和机器人是否开口

如果 Cookie 过期（约 2 个月后，或提示未登录）：
`RL_BROWSER=edge node login-and-grab.mjs` 重新扫码，会覆盖 `cookie.txt`，
再把它同步进 `bin/config.yaml` 的 `cookie.douyin`（或直接改那一行）。

## 事件格式

stdout 一行一个 JSON，人类可读日志全部走 stderr：

```json
{"ts":1794556452069,"type":"chat","user_id":"107776453197","user_name":"小宇宙","text":"主播多大了","msg_id":"..."}
{"ts":1794556452074,"type":"gift","user_id":"456","user_name":"小红","gift":"玫瑰","gift_id":"1","repeat":1,"combo":3,"diamond":1,"msg_id":"..."}
{"ts":...,"type":"enter","user_id":"...","user_name":"..."}
{"ts":...,"type":"like","user_id":"...","user_name":"...","count":12}
{"ts":...,"type":"meta","event":"start|reconnect","room":"..."}
```

`--only=gift,chat` 可只保留关心的类型。

## 礼物规则（gifts.json）

改完**不用重启**，pipeline 每 200 ms 检查 mtime 自动热加载。

| 机制 | 解决什么 |
|---|---|
| `msg_id` 去重 + `repeat` 递增放行 | 连击重复推送，但不能把「连刷 10 个」砍成 1 个 |
| `combo_window_ms` 连击汇总 | 一个人刷 10 个只谢一次，报总数 |
| `cooldown_ms_per_user` | 一个人刷屏霸占机器人（冷却期内不打断别人） |
| `tiers` 按钻石数分档 | 大小礼物语气不同，优先级 8/9/10 |
| `forbidden_words` | 昵称带脏词 → 换称呼；整句仍命中才丢弃 |

## 机器人 RPC 的三个坑（都已处理，改代码时别踩回去）
1. **`service_name` 不是统一加前缀**。只有 `CANONICAL` 集合里的服务用
   `/aimdk_5Fmsgs/srv/Xxx`，`PlayTts` / `SetTtsParameters` 用原始名。
2. **`1007 服务未启用` 是瞬时态**，不是永久失败。它是明确拒绝、无副作用，
   可以安全重试（`speaker.py` 重试 2 次，间隔 0.6 s）。**超时绝不重发**，会重复播报。
3. **`priority_rejected`**：高优先级（如 p10 大礼物）正在播时，p8 会被直接拒绝。
   这也是无副作用拒绝，`pipeline.py` 会降级为不打断、延后 1.8 s 重提，最多 3 次。

判断依据都来自回执的 `tts_resp.error_message`，不要只看 `is_success`。

### 还有一个环境坑：密钥别放 `~/.ssh`

`speaker.py` 原来默认读 `~/.ssh/qiyuan_t1`。GUI 或服务化运行时时，子进程会报：

```
Load key "C:\Users\anstar/.ssh/qiyuan_t1": Permission denied
run@192.168.50.9: Permission denied (publickey)
```

文件本身是可读的（Python 能打开），是**受限运行环境读不到用户主目录**。
现在密钥搜索顺序是：`QIYUAN_KEY` 环境变量 → 项目内 `secrets/qiyuan_t1` → `~/.ssh/qiyuan_t1`，
实际放 `secrets/qiyuan_t1`（600）即可。另外隧道已加
`UserKnownHostsFile=/dev/null + BatchMode=yes + LogLevel=ERROR`，
不再尝试写 known_hosts，也不会卡在交互提示上。

## 目录

```
go_collector.py         采集适配层：连 douyinLive 的 WS，归一化成统一事件（当前主用）
bin/                    douyinLive v2.3.2 单文件服务 + config.yaml（Cookie 在这里）
                        ↑ 礼物消息只能从这里来，自建栈拿不到
collector.py            自建栈采集器（--source py 时用，保留作对照/兜底）
diag_methods.py         探针：统计服务端实际下发的每种 msg.method，排除"被静默吞掉"
login-and-grab.mjs      受控登录窗口：扫码一次，自动抓 Cookie 写 cookie.txt
grab-cookie-edge.mjs    从 Edge 配置读 Cookie（受 App-Bound 加密限制，现已不用）
decrypt_cookies.py      尝试直接解 Chrome/Edge 的 v20 Cookie（受 ABE 限制，留作记录）
set_cookie.py           手动写入 Cookie（清洗 + 校验 sessionid）
cookie.txt              登录态（600 权限，勿外传，勿提交）
watch.py                实时看板：连上房间把弹幕/礼物打在屏幕上，结束出汇总
rules.py                礼物规则引擎（去重/连击/冷却/话术渲染）
speaker.py              播报通道（本机直连 / SSH 隧道两种模式，可独立使用）
pipeline.py             总装：采集 -> 规则 -> 播报（--source go|py）
admin.py                控制台网页（临时播报 / 不回答清单 / 暂停）
bans.py                 不回答清单（bans.json 热加载）
verify.py               一次性验靶：跑 N 秒出判决表
gifts.json              话术表（可随时改）
bans.json               不回答清单数据
replay-sample.json      假事件样本，用于不连抖音自测
runs/                   原始事件 / 日志落盘
vendor/src/             自建栈引擎（liveMan.py，参数写死，勿用于收礼物）
probes/                 摸底脚本，不参与生产
```

## 待办

1. ~~验证真实礼物事件~~ **已完成**：`WebcastGiftMessage` 2 条（人气票），见上方「关键突破」
2. **明天做**：真礼物 → 机器人开口（机器人今天没连上，这一步未跑）
3. 采集看门狗：超过 N 秒无消息 → 告警 + 重连 + 兜底话术
4. 弹幕问答环（本轮不做，只采集不回复）
5. 部署形态：`douyinLive` 服务 + `go_collector.py` + `pipeline.py` 三段一起上机器人本体，
   需 systemd 自启 + 日志轮转（`douyinLive` 有 linux-arm64 版，可直接用）
6. Cookie 过期自动提醒（有效期约 2 个月，可读 `expires_utc` 提前告警）

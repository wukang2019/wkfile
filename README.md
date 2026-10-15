# 让 AI 远程管理 Windows 服务器文件（wkfile）

让 AI 客户端（**Trae / Cursor / Claude Desktop** 等一切支持 MCP 的工具）
直接**查看和修改**你 Windows 服务器上的文件：在对话框里说  
「把 `E:\apps\config\app.php` 里的超管账号改一下，改完贴出来」，AI 就会自己去服务器上读、改、写。

**服务器上跑一个 exe，客户端里配一行 JSON，完事。** 不需要装 sshd、不需要隧道、不需要在开发机放桥接程序。

> 本文档统一用「客户端」指代任意 MCP 客户端。涉及具体菜单位置时以 **Trae** 举例，
> Cursor 是 `设置 → MCP`、Claude Desktop 是 `claude_desktop_config.json`，字段含义完全一样。

---

## 1. 接入方式（只有一种：IP 直连）

```
┌── 开发机（装 MCP 客户端）──────────────────────────────────┐
│  Trae / Cursor / Claude ── MCP over HTTP / SSE ─┐           │
│  本机不用装任何东西                            │            │
└────────────────────────────────────────────────┼────────────┘
                                                 │ HTTP(S) + Bearer Token
                                                 ▼
┌── Windows 服务器 ──────────────────────────────────────────┐
│  wkfile.exe（常驻服务，可开机自启）                  │
│    ├─ MCP 端点    /mcp（Streamable HTTP）· /sse（SSE）      │
│    ├─ 通用接口    /api/{roots,disk,list,read,write,...}     │
│    ├─ Token 鉴权 · IP 白名单 · 失败锁定                     │
│    ├─ 根目录白名单（越界一律 out_of_scope）                 │
│    ├─ 写前自动备份 → wkfile\backups\                      │
│    ├─ 删除进回收站 → wkfile\trash\（可恢复）              │
│    └─ 审计日志 → wkfile\logs\audit.jsonl                  │
│                                                             │
│  实际文件：E:\apps\…                                     │
└─────────────────────────────────────────────────────────────┘
```

> **已实测打通**（2026-10-14）：从开发机经公网直连服务器 MCP 端点，  
> `initialize` → `tools/list`（33 个工具）→ `win_roots` → `win_list` 全部成功，  
> 真实读到了服务器上 `E:\apps` 的目录列表。

### 端口说明（重要）

| 端口        | 说明                              |
| --------- | ------------------------------- |
| **10087** | **你服务器上实际在用的端口**，公网直达，客户端就填这个 |
| 8788      | 程序内置的默认值，只在界面上没改过时生效            |
| 22        | 与本事无关，**不要**去连它（连它必然超时）         |

程序界面里填什么端口，就以什么为准。下面示例统一按 `10087` 写。

---

## ★ 先回答最常被问的一件事：到底要不要装 Python？

**看你走哪条路线，两者程序能力完全一样，区别只是「Python 解释器放在哪」。**

| 你走哪条路线 | 服务器上要装 Python 吗 | 还要装别的东西吗 |
| ------------ | -------------------- | -------------- |
| **单文件 `wkfile.exe`**（推荐、部署包里的那个） | **不用**。Python 解释器已经被打进 exe 内部了 | 不用 |
| 源码 `wkfile_agent.py` | 要，**Python 3.8+**（用 python.org 官方安装包，自带 tkinter） | 不用 |

- **任何情况下都不需要 `pip install`。** 全项目零第三方依赖 —— 从上面 `server\` 里
  的 import 可以逐个核对，全部是 Python 标准库 + 本项目自己的三个 `.py`。
  连自签证书都是手写 ASN.1/DER + RSA 生成的，**不需要 openssl、不需要 cryptography**。
- **exe 路线为什么不用装 Python**：PyInstaller 打包时把解释器和用到的标准库
  一起塞进了 `wkfile.exe`。所以它在服务器上是「解压即用」的绿色程序，
  不写注册表、不改环境变量、不装运行库。
- **3.8 是硬下限，原因只有一个**：`server\wkfile_selfcert.py` 用
  `pow(a, -1, m)` 求模逆来生成 RSA 密钥，这个写法 Python 3.8 才支持。
  其余代码用到的都是 3.7 就有的东西。
- **唯一的系统前提**：Windows 10 / Server 2016 及以上（自带
  `api-ms-win-crt-*` 通用 C 运行库）。更老的系统需要先补一个
  VC++ 2015-2022 运行库，仅此一项。

> 换句话说：**「纯标准库」讲的是"不装包"，不是"不装 Python"。**
> 想连 Python 都不装，就用部署包里的 `wkfile.exe`。

---

## 2. 目录结构

```
trae-winfile\
├─ wkfile-v1.6.4\                  # ★★ 部署包：整个文件夹拷到服务器就能用
│   ├─ wkfile.exe                  #   主程序（免装 Python）
│   ├─ 文件夹说明.txt               #   先看这个：包里每个文件是干什么的
│   ├─ 使用说明.txt                 #   从零到跑通的全流程（纯文本）
│   ├─ README.md                   #   完整文档
│   ├─ 以管理员身份启动.bat          #   带 UAC 提权地打开配置界面
│   ├─ connect-check.py / 连接自检.bat   # 连接自检（★ 必须与 exe 同级）
│   ├─ wkfile.config.example.json  #   配置样例
│   ├─ wkfile\                     #   ★ 运行时数据目录（配置/日志/备份/回收站）
│   └─ tools\                      #   连通性诊断脚本
├─ wkfile-v1.6.4.zip               # 上面那个文件夹的同内容压缩包
│
├─ dist\
│   └─ wkfile.exe                  # 编译产物（部署包从这儿取）
├─ server\                         # ★ 源码：服务器端
│   ├─ wkfile_agent.py             #   服务主体（纯标准库，也可直接用 Python 跑）
│   ├─ wkfile_mcp_endpoint.py      #   MCP 协议层（/mcp 与 /sse，被 agent 调用）
│   ├─ wkfile_selfcert.py          #   自签证书生成器（纯标准库，手写 DER/RSA，不依赖 openssl）
│   ├─ wkfile_mysql.py             #   MySQL/MariaDB 驱动（纯标准库手写协议，不依赖 PyMySQL）
│   ├─ wkfile_redis.py             #   Redis 驱动（纯标准库 RESP，不依赖 redis-py）
│   ├─ wkfile_gui.py               #   图形界面源码（tkinter）
│   ├─ connect-check.py            #   连接自检：配置/端口/IP/防火墙/MCP 一次查完
│   ├─ 连接自检.bat                 #   上面那个的启动器，服务器上双击即可
│   ├─ wkfile.config.example.json  #   配置样例（真配置在 wkfile\wkfile.config.json）
│   └─ install-server.ps1          #   纯 Python 部署路线的一键脚本（纯 ASCII）
├─ tools\                          # 连通性诊断脚本（部署包里也有一份）
│   ├─ probe-public.py             #   探端口开没开
│   ├─ mcp-public-test.py          #   验 MCP 端点（initialize + tools/list）
│   └─ mcp-call-test.py            #   真调一个工具（如 win_roots / win_list）
├─ tests\                          # 端到端测试套件（本地开发用）
│   ├─ features_e2e.py             #   新增能力自测（自签证书、公网 IP、证书链、数据库、超时边界，227 用例）
│   ├─ mcp_e2e.py                  #   MCP 端点（Streamable HTTP + SSE + 鉴权 + 越权拦截）
│   ├─ local_e2e.py                #   本机端到端（源码路线）
│   ├─ tls_e2e.py                  #   真 HTTPS 链路（需本机有 openssl，无则跳过）
│   └─ mcp_exe_test.py             #   MCP 端点（打包后的 exe，需先 build）
├─ deprecated\                     # 已废弃方案归档，见里面的 README.txt
├─ .trae\mcp.json                  # Trae 项目级 MCP 配置（含真实 Token，勿提交 git）
├─ selftest.py                     # 老接口 + 安全机制 + MCP 协议自测（53 用例）
├─ build-exe.ps1                   # 重新编译 exe
├─ build-release.py                # 重新生成部署包文件夹 + zip
├─ LICENSE                         # MIT
└─ README.md
```

**部署时只需要 `wkfile-v1.6.4\` 这一个文件夹**（或者解压同名 zip），
其余的 `server\`、`dist\`、`tools\` 等都是开发用的源码与中间产物。

### 运行时只创建一个文件夹：`wkfile\`

```
wkfile\                          ← 程序启动时唯一会创建的地方（就在 exe 旁边）
├─ wkfile.config.json            配置（含 Token，别外传）
├─ wkfile.pid                    服务进程号
├─ logs\wkfile.log               运行日志
├─ logs\audit.jsonl              审计日志（超 5 MB 轮转，留 3 份）
├─ backups\<时间戳>\<根目录>\…    改写文件前的自动备份
└─ trash\<trash_id>\…            删除的东西（可用 trash_id 恢复，不是真删）
```

- **选址规则**：exe 所在目录若已叫 `wkfile` 就直接用（不套娃）；否则在 exe 同级
  建 `wkfile\`；两处都不可写（如装在 `C:\Program Files`）才退到 `%PROGRAMDATA%\wkfile\`。
- 备份与回收站也收在这里，**不再**往被操作的业务目录里撒 `.winfile-*` 目录。
- `wkfile\` 自身、配置文件、日志、备份、回收站都**不允许通过接口访问** ——
  否则 AI 读一次配置就拿到 Token，鉴权形同虚设。

> 为什么部署包里 `connect-check.py` 和 exe **同级**而不放进 `server\` 子目录：
> 它靠"自己旁边"去找 `wkfile\wkfile.config.json`。放进子目录会找不到配置、
> 静默回退到内置默认端口 8788，然后给出完全错误的结论。

重新生成部署包（改完源码 / 重编译 exe 之后）：

```powershell
powershell -ExecutionPolicy Bypass -File .\build-exe.ps1   # 先重编译 exe
python build-release.py                                     # 再重建 文件夹 + zip
```

---

## 3. 服务器侧：3 步

| 文件                                  | 放哪              | 干什么                                |
| ----------------------------------- | --------------- | ---------------------------------- |
| **`wkfile-v1.6.4\` 整个文件夹** | **Windows 服务器** | 拷过去就行，里面含 exe + 说明书 + 自检脚本 + 工具 |

单文件绿色版：不安装、不写注册表，卸载就是删掉。**建议放 `D:\wkfile\` 这类普通目录**；  
放 `C:\Program Files\` 的话配置和日志会自动改写到 `C:\ProgramData\wkfile\`。

> 首次双击若提示「未知发布者」（没买代码签名证书，不是病毒），点  
> 「更多信息」→「仍要运行」。若被**智能应用控制（SAC）**&#x76F4;接拦死且没有「仍要运行」按钮，  
> 那台机器只能跑源码版（`启动界面.bat` + Python），或用已放行的旧版 exe。

打开界面有两种方式：直接双击 `wkfile.exe`，或双击 **`以管理员身份启动.bat`**（自动提权，
省得右键选）。装开机自启、放行防火墙需要管理员权限，界面分三页：

| 页           | 你要做的事                                                                                                                                                             |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **① 基础设置**  | 监听地址选 `0.0.0.0`（客户端在别的机器上）；端口填 **10087**；「添加目录…」把允许 AI 操作的目录加进白名单（**越窄越好**）                                                                                     |
| **② 安全与权限** | 点「生成」拿 Token；设有效期（「永久」或 7/30/90 天，到期自动失效）；填来源 IP（支持 `192.168.1.*`、`10.0.0.0/8`、`203.0.113.45`）；按需勾只读/允许删除/允许移动/覆盖前备份/写前语法校验；**「开放 MCP 端点」保持勾选**；下半部分**工具级开关**可以逐个关掉不想让 AI 用的工具（想「只查看」就点「只留只读」） |
| **③ 安装与运行** | 依次：`保存配置` → `启动服务` → `安装开机自启` → `放行当前端口`                                                                                                                          |

- 页签③底部还有**审计日志轮转**设置（默认超过 5 MB 滚动、保留 3 份）和「清空审计日志」。
- 若监听地址不是本机、又允许删除/移动，启动前会弹一次**高风险确认**；  
  而「监听 0.0.0.0 + Token 是空的或示例占位值」会被**直接拒绝启动**（见第 7 节）。
- 每次操作的结果都会显示在窗口**顶部那条浅色状态条**上（绿=成功 / 黄=提醒 / 红=失败），关键动作还会弹窗。
- 云服务器还要去控制台**安全组放行同一个端口**，否则本机防火墙放行了也白搭。
- 点「复制 MCP 配置」→ 剪贴板里就是填好地址和 Token 的 JSON。  
  **服务器地址会自动填好**：程序启动时会探测本机公网 IP 并写进这一栏
  （客户端在另一个网络时，内网 IP 一定连不上）。同局域网用内网 IP 更快，
  或探测失败时可以点「检测公网 IP」重试，也可以直接手改这一栏。

配置、日志、备份、回收站全部收在 exe 旁边的 **`wkfile\`** 文件夹里（见第 2 节「运行时只创建一个文件夹」）。

---

## 4. 客户端侧：1 步

把上面复制的 JSON 粘进客户端：

| 客户端             | 位置                                                        |
| --------------- | --------------------------------------------------------- |
| **Trae**        | 设置 → MCP → 添加 → 手动配置 → 原始配置；或项目根目录 `.trae\mcp.json`     |
| **Cursor**      | 设置 → MCP → Add new MCP server；或项目根目录 `.cursor\mcp.json`     |
| **Claude Desktop** | `claude_desktop_config.json`（设置里点「编辑配置」直接打开）               |
| 其它             | 只要支持 **Streamable HTTP** 或 **SSE** 这一行 JSON 就通用             |

**Trae** 的写法（项目级配置，也可以直接在设置界面粘贴）：

```json
{
  "mcpServers": {
    "wkfile": {
      "type": "http",
      "url": "http://1.2.3.4:10087/mcp",
      "headers": { "Authorization": "Bearer 你的Token" }
    }
  }
}
```

配好后客户端的 MCP 列表里 `wkfile` 显示 **Running**，工具列表里能看到 33 个 `win_*` 工具。

> 若客户端只认 SSE，把 `type` 改成 `"sse"`、`/mcp` 换成 `/sse`——服务端两个端点都开着。  
> Trae 打开的是工作区根目录（如 `D:\my-projects`）时，项目级配置要放在  
> **那个根目录**的 `.trae\mcp.json`，放在 `trae-winfile\.trae\` 里它读不到。

---

## 5. 验证与排错

### 服务器本机（双击 `连接自检.bat`，就在 exe 旁边）

> 从源码目录跑的话是 `server\连接自检.bat`；部署包里它和 exe 同级 ——
> 两种布局都能找到 `wkfile\wkfile.config.json`（旧版的 `agent.config.json` 也兼容）。

一次告诉你：配置对不对、服务在没在跑、**内网有哪些地址、公网 IP 是多少**、防火墙放行没、  
以及可直接粘贴的客户端配置（默认优先给公网 IP）和开发机该测的命令。

### 任意机器（用 tools\ 里的脚本）

```bash
python tools/probe-public.py 1.2.3.4                  # 端口开没开
python tools/mcp-public-test.py http://1.2.3.4:10087 <Token>   # MCP 握手
python tools/mcp-call-test.py  http://1.2.3.4:10087 <Token> win_roots
```

### 手测 MCP（服务器本机）

```powershell
curl.exe http://127.0.0.1:10087/api/health

curl.exe -X POST http://127.0.0.1:10087/mcp `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer <Token>" `
  -H "Accept: application/json, text/event-stream" `
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}"
```

### 症状对照

| 现象                     | 原因                                             |
| ---------------------- | ---------------------------------------------- |
| `/mcp` 返回 **401**      | Token 不对（或少了 `Bearer ` 前缀）                     |
| `/mcp` 返回 **404**      | 配置里 `enable_mcp = false`，回界面勾上「开放 MCP 端点」再保存重启 |
| `Connection timed out` | 端口 / 防火墙 / 云安全组 / 目标 IP 错                      |
| `out_of_scope`         | 请求路径不在白名单目录里，这是**正常的越权拦截**                     |
| 按钮点了没反应                | 看窗口**顶部状态条**和底部日志；端口检查、启动服务这类关键动作会弹窗           |

---

## 6. 可用工具（33 个）

**文件类（23 个）**

| 工具                    | 作用                                 |
| --------------------- | ---------------------------------- |
| `win_roots`           | **先调这个**：看能操作哪些目录、是否只读、这次连接是不是只读 Token |
| `win_disk`            | 各磁盘容量/剩余空间                         |
| `win_list`            | 列目录（支持递归、通配符过滤）                    |
| `win_tree`            | 一次拿整个目录树概览（带缩进与大小），摸项目结构用          |
| `win_info`            | 单个文件/目录详情，可算 sha256                |
| `win_read`            | 读文本（带行号，可分段读大文件，自动识别 UTF-8/GBK）    |
| `win_tail`            | **只看文件末尾 N 行** —— 查日志、看最新报错，不必读整个文件  |
| `win_multi_read`      | 一次读多个文件（配置+日志+代码），单个失败不影响其它        |
| `win_write`           | 写/覆盖文本（自动建目录、写前备份、可带 sha256 防并发覆盖） |
| `win_replace`         | 精确字符串/正则替换；支持 `dry_run` **先看 unified diff 预览** |
| `win_append`          | 追加内容（写日志）                          |
| `win_mkdir`           | 建目录                                |
| `win_move`            | 移动/重命名                             |
| `win_copy`            | 复制文件/目录                            |
| `win_trash`           | 删除 → **进服务器回收站**，返回 `trash_id`     |
| `win_trash_list`      | 看回收站                               |
| `win_trash_restore`   | 从回收站恢复                             |
| `win_backup_list`     | **看文件的历史备份**（每次覆盖写入前自动生成）          |
| `win_backup_restore`  | **回滚到某个备份版本**（改坏了用它自救）             |
| `win_search`          | 按文件名递归查找（支持 `*` `?` 或正则）           |
| `win_grep`            | 按正则搜文件内容（列日志、找报错）                  |
| `win_read_b64`        | 读小二进制文件（base64）                    |
| `win_write_b64`       | 写二进制文件（base64）                     |

**数据库/缓存类（10 个，可选）**

| 工具                    | 作用                                 |
| --------------------- | ---------------------------------- |
| `win_db_list`         | **先调这个**：看配了哪些数据源、这次有没有写权限         |
| `win_db_test`         | 测连通性：服务端版本、认证方式、当前身份、可见的库          |
| `win_db_query`        | MySQL/MariaDB **只读**查询（SELECT/SHOW/DESC/EXPLAIN） |
| `win_db_exec`         | MySQL/MariaDB **写**语句（需要数据源开启允许写入）  |
| `win_db_tables`       | 列库中的表（引擎、估算行数、占用空间、注释）             |
| `win_db_schema`       | 看某张表的完整列定义与索引                      |
| `win_redis_info`      | Redis 概况：版本、连接数、内存、各库 key 数        |
| `win_redis_keys`      | 按模式列 key（走 `SCAN`，**不会**像 `KEYS` 那样阻塞实例） |
| `win_redis_get`       | 读单个 key，按实际类型自动展开（string/hash/list/set/zset/stream） |
| `win_redis_write`     | 执行 Redis 写命令（需要数据源开启允许写入）           |

> **每个工具都带了 MCP 标准风险标注**（`readOnlyHint` / `destructiveHint` /  
> `idempotentHint` / `openWorldHint`）。这不只是文档：MCP 规范里"不标注"的默认值是  
> **破坏性 + 面向开放世界**，也就是说只读工具会被客户端当成危险操作来提示。  
> 现在 21 个只读、12 个写入各自标注准确，客户端那边的安全提示才是对的。

> **备份与回收站的区别**：`win_trash` 是"我要删掉它"，进回收站；  
> `win_backup_*` 是"它被改过，我要看以前的版本"，读的是 `wkfile\backups\`。  
> 两者互不相干，回滚前也会先把当前位置再备份一份，所以**回滚本身也可撤销**。

> **所有 MCP 工具调用都写审计日志**（含只读 Token 请求的 `scope=readonly` 标记）。

> **`win_replace` 有一条护栏**：不带 `count` 时，如果 `old` 在文件里出现多次，  
> 本次调用会被**拒绝**（`ambiguous_match`），要求补更长的唯一上下文或显式指定 `count`。  
> 目的是避免"只想改一处 `localhost`，结果文件里 4 处全被改掉"这类不可逆误伤。

### 数据库工具怎么开

默认**一个数据源都不配，数据库能力就是完全关闭的**。要开启：启动界面 → **④ 数据库** →
「＋ MySQL」或「＋ Redis」→ 填地址账号 → **「测试连接」** → 「保存配置」→ 重启服务。

**「到底在哪儿填数据库？」** —— 两个地方，改哪个都一样，随便挑：

| 方式 | 怎么做 | 适合 |
| --- | --- | --- |
| **图形界面**（推荐） | 双击 `wkfile.exe` → 「④ 数据库」页 → 点两下 | 第一次配 |
| **直接改配置文件** | 记事本打开 `wkfile\wkfile.config.json`，改 `databases` 段 | 批量改、无桌面环境 |

配置文件**程序会自动生成**，路径固定在 exe 旁边的 `wkfile\wkfile.config.json`。
第一次运行 exe 时它就会落盘，而且**自带中文注释和可照抄的示例**，打开就能看懂：

```jsonc
  "databases": [],
// ★ 数据库 / 缓存就在这里加。想开就照着下面这段抄，抄完删掉 // 即可：
//   "databases": [
//     {"name":"prod","type":"mysql","host":"127.0.0.1","port":3306,
//      "user":"root","password":"","database":"mydb","allow_write":false,
//      "max_rows":200,"timeout_sec":30,"connect_timeout_sec":8},
//     {"name":"cache","type":"redis","host":"127.0.0.1","port":6379,
//      "password":"","username":"","db":0,"allow_write":false,
//      "max_keys":200,"timeout_sec":15,"connect_timeout_sec":8}
//   ],
```

> 这些 `//` 注释是程序自己写的，读的时候会自动跳过，**不会**把 JSON 解析弄坏。
> 你手改时也可以照这样加注释，一样能读。
> 只改了 `databases` 这一段的话，保存后**重启服务**即可，其它项不用重新配。

想验证配置对不对，可以直接问 AI：

```
先 win_db_list 看有哪些数据源，然后 win_db_tables 列出 prod 库里所有的表；
最后 win_db_schema 看一下 users 表的结构，我要写查询。
```

**权限是三重闸门，缺一不可**（这是刻意的，避免一次误配就把生产库交出去）：

1. 数据源自己的「允许写入」开关（默认关）
2. 服务端全局 `read_only` 不为真
3. 本次请求用的是**主 Token**，而不是只读 Token

任何一条不满足，写语句都会被拒绝并说明是哪一条挡的。

**语句分级**（MySQL 侧）：

| 级别 | 判定 | 处理 |
| --- | --- | --- |
| 只读 | `SELECT` / `SHOW` / `DESC` / `EXPLAIN` / `WITH...SELECT` | 直接放行 |
| 写 | `INSERT` / `UPDATE` / `DELETE` / DDL / `SET` 等 | 需三重闸门全开 |
| 拒绝 | 多语句、`INTO OUTFILE`、`LOAD_FILE`、`FOR UPDATE`、`SLEEP()` 等 | 任何情况都不放行 |

只读判定的实现细节值得说明：判定前会先**剥离注释与字符串字面量**，所以
`SELECT 'drop table x'` 不会被误判成删表，`/* x */ DELETE` 也不会被漏掉。
另外只读查询还会在会话上执行 `SET SESSION TRANSACTION READ ONLY` —— 让**数据库自己**
兜底，就算分类器被骗过，写入依然会被 MySQL 拒绝。

**Redis 侧用白名单**：只有明确列在只读清单里的命令才放行，其余一律按写处理，
`FLUSHALL` / `FLUSHDB` / `CONFIG` / `EVAL` / `MONITOR` / `SHUTDOWN` /
`SUBSCRIBE`（会改变连接应答模型）等永远拒绝。列 key 强制走 `SCAN`，
**`KEYS` 被禁用**——它会阻塞整个实例。

> 驱动是**纯标准库手写**的（`wkfile_mysql.py` / `wkfile_redis.py`），
> 依然不需要 `pip install`，也不依赖 `mysql.exe` / `redis-cli`。
> MySQL 8 默认的 `caching_sha2_password`（含首次连接的 RSA 公钥交换）已完整支持，
> MariaDB / MySQL 5.7 / Redis 3.x 以上都验证过。

**两个超时是两个概念，别混**（界面④里是两个独立的框）：

| 字段 | 含义 | 默认 | 什么时候要调 |
| --- | --- | --- | --- |
| `connect_timeout_sec` | **拨号预算**：域名解析 + 逐个地址 TCP 连接，总共就这么多秒 | 8 | 库在跨地域/公网、建连慢，或想让它更快失败 |
| `timeout_sec` | **查询超时**：连上之后等结果的时间 | MySQL 30 / Redis 15 | 有大表慢查询，或想限制单次调用时长 |

为什么要自己写拨号逻辑而不用 `socket.create_connection`：后者对
`getaddrinfo` 返回的**每个**地址都用一整份 timeout 各试一次。一台既有 A 又有 AAAA
记录的机器，只要 IPv6 那条路是黑洞（很常见：能解析、路由不通），
"配了 8 秒"实际要等 16 秒才报错。现在改成自己解析、每个地址只花**剩余预算**，
实测 3 个黑洞地址 / 2 秒预算：旧写法 6.0 秒，新写法 2.0 秒。

另外配置里的秒数一律会被**夹取**：`0`、负数、`true`、`"abc"` 都退回默认值，
超过 300 秒夹到 300。这不是洁癖——`sock.settimeout(-1)` 抛的是 `ValueError`
而不是 `DBError`，会一路冒成"内部错误"，那不叫报错，那叫查不出来。

> 唯一还留在预算之外的环节是 **DNS 解析本身**（`getaddrinfo` 不能被中断，
> 纯标准库没法给它设超时）。所以真配了一个解析不出来的域名时，
> 等待时间由系统解析器决定，通常是秒级失败。

### 对话示例

```
先 win_roots 看看能操作哪些目录，然后 grep 一下 E:\apps\logs\app.log 里的
"OutOfMemory"，把命中的上下文给我。
```

```
读 E:\apps\config\database.php，把 db.host 从 127.0.0.1 改成 10.0.0.5，
改完把整份配置贴出来。
```



---

## 7. 安全清单（**务必看**）

这个服务能**读写服务器真实文件**，且当前是公网暴露，就靠一道 Token。

1. **Token 当密码对待**：一旦在聊天记录、截图、日志里出现过，立刻在界面点「生成」换新的，  
   并同步更新客户端配置。`.trae/mcp.json` 和 `server/wkfile/wkfile.config.json` 都已在 `.gitignore` 里，  
   别提交进版本库。
2. **给 Token 设个有效期**：界面②的「有效期」填 7 / 30 / 90 天即可（默认「永久」）。
   到期那一刻起服务直接回 401 `token_expired`，即便 Token 早就在某处泄露了，
   也只在有限窗口内可用。命令行续期（会换新 Token，旧的立即失效）：
   `python wkfile_agent.py --renew-token --token-days 30`
3. **收窄「限制目录」**：只给真正需要远程改的子目录，别图省事填 `D:\`、`C:\` 根目录。  
   系统目录（`C:\Windows`）**永远不要**加。生产项目目录里如果有 `config/`、  
   `test_db_connect.php` 这类存数据库凭据的文件，更要小心——现在这个服务对白名单目录  
   **有删除和移动权限**。
4. **只读优先**：只是想让 AI 看代码，就在界面勾「只读模式」，或关掉「允许删除 / 允许移动」。  
   当前建议至少关掉删除和移动。
5. **来源 IP 白名单**：界面②填你办公网/家庭出口 IP，支持 `x.x.x.*` 和 CIDR。  
   留空 = 不限制，公网服务器不建议留空。
6. **云安全组**：把该端口的来源限制成你的出口 IP，不要留 `0.0.0.0/0`。  
   家用宽带 IP 会变，变了就更新。
7. **上 HTTPS —— 程序自己就能做，证书也能自己签（v1.6.0 起）**：现在是 `http://`，
   **Bearer Token 明文过公网**。  
   不用装 Apache、不用配反代：界面③「HTTPS」栏勾上、选好证书和私钥、点「检查证书」，
   保存重启即可。服务会直接在端口上做 TLS，客户端那边把地址改成 `https://` 就行。

   **证书从哪来 —— 界面上三个框，照着证书商给你的文件对号入座就行**：

   | 证书商给你几份 | 界面上怎么填 |
   | -------------- | ------------ |
   | **只有 IP、没有域名**（最常见） | 点「**生成自签证书**」，三个框都不用管 |
   | `crt` + `key` 两份 | 第一行填 crt，第三行填 key，**中间链留空** |
   | `public` + `chain` + `key` **三份** | 依次填进三个框（公钥证书 / 中间证书链 / 私钥） |
   | 一份 `fullchain` / `证书链`（里面已有 2~3 张） | 第一行填它，中间链留空 |

   - **点「生成自签证书」时**：程序现场签一张，纯本地运算，
     **不装 openssl、不装任何 Python 包、不联网**，通常几秒（RSA 密钥生成时间本身有波动）。
     生成时会自动把 `127.0.0.1` + 本机内网 IP + 探测到的公网 IP
     **全部写进 SAN**（少写一个，客户端按那个 IP 连就必然校验失败），
     生成完自动回填路径、自动勾上 HTTPS、自动跑一次「检查证书」。
   - **三件套为什么能塞进三个框**：Python 的 `load_cert_chain()` 允许一个证书文件里
     依次放多张 PEM 证书。所以程序启动时会自动把「公钥证书 + 中间证书链」拼成一份
     fullchain（顺序是站点证书在前、中间 CA 在后 —— 反了客户端会直接报错），
     落在 `wkfile\tls\fullchain.pem`，再交给 TLS 加载；出问题时可以直接打开这个文件核对。
     工作目录不可写时会退回系统临时目录，**不会因为这个起不来**。

   ### 最容易踩的坑：证书链不完整

   证书商后台通常同时给「单张 crt」和「带链的 chain/fullchain」两种文件，
   **选错的那一瞬间是看不出来的**：

   - 服务端只会把文件里的证书发出去。只发一张（站点证书）时，
   - **浏览器和 Windows 会顺着证书里的 AIA 地址自动把中间证书补回来**，
     所以你在浏览器里访问是正常的、完全看不出问题；
   - 但 **Trae / Cursor / Claude Desktop 这类 Electron 客户端走 Node 的 TLS 栈，
     不补链**，会直接报 `unable to verify the first certificate` 而连不上 ——
     表现就是「浏览器能开、客户端连不上」。

   程序对这种情况有**三重提醒**，不用你自己去猜：

   1. 界面点「检查证书」会当场标黄：`⚠ 配对正常，但证书文件里只有 1 张证书，没带中间证书`；
   2. 服务启动横幅会打 `[!] 证书文件里只有 1 张证书，没带中间证书`；
   3. 服务器上跑 `连接自检.bat`（`connect-check.py`）会直接判红并给出两种改法。

   改法就是上面的表格：**要么第一行换成 fullchain，要么把链文件填进中间链那一栏。**

   自签证书客户端首次连接会提示不受信任，确认一次即可（之后不再提示）。
   **源码版**（`python wkfile_agent.py`）也可以从命令行生成 ——
   适合没有桌面、打不开界面的服务器：

   ```powershell
   # 默认写到 wkfile\tls\ 下；自动带上本机所有 IP
   python wkfile_agent.py --gen-selfcert
   # 想自定义位置/CN/额外 IP（逗号分隔，可以是域名）
   python wkfile_agent.py --gen-selfcert --selfcert-dir D:\certs ^
       --selfcert-cn wkfile --selfcert-ip 1.2.3.4,wk.example.com --selfcert-days 3650
   ```

   > 图形版 `wkfile.exe` **不接**这些命令行参数（它是 windowed 编译的，没有控制台，
   > 执行了你也看不到结果）。给 exe 加这些参数时会弹窗说明并指向界面里的按钮，
   > 不会静默地"看起来什么都没发生"。

   只有一条硬要求：**证书和私钥必须能配对加载**。界面上的「检查证书」就是当场加载一次，
   不通过就不让保存 —— 避免配了个坏的证书、重启后服务起不来。
   > 不想用自带 TLS、坚持走 Apache 反代也可以（`ProxyPass /wkfile/ http://127.0.0.1:10087/`），
   > 但那样每次改配置都要动 Apache，且 `jz.dengtekj.cn` 的证书 2026-10-03 已过期，需先续期。
8. **能让 AI「只看不改」就给个只读 Token**（v1.6.0 起）：界面②「只读 Token」栏点「生成」，
   把这一串单独给到"只想让它读代码"的客户端。用它的请求**写 / 删 / 移 / 回滚全部被拒**
   （错误码 `read_only`），而主 Token 权限不受影响。比全局开 `read_only` 灵活 ——
   可以让一个客户端用只读 Token 去看，同时另一个客户端用主 Token 去改。
   命令行生成：`python wkfile_agent.py --gen-readonly-token`
9. **服务端自带的多层防护**（已实现，并实测过）：
   | 机制             | 说明                                                                                                                              |
   | -------------- | ------------------------------------------------------------------------------------------------------------------------------- |
   | 根目录白名单         | 路径 resolve 后必须在 roots 内；`..` 穿越、`C:\Windows` 越界实测均被拒                                                                            |
   | 备用数据流/设备路径     | `file.txt:ads`、`\\.\` 一律拒绝                                                                                                      |
   | 删除保护           | 根目录本身不可删；非空目录要显式 `recursive=true`；删除只是移进回收站                                                                                     |
   | 写前备份           | 覆盖/替换前自动拷到 `wkfile\backups\<时间戳>\`                                                                                            |
   | **写前语法校验**（新）  | `.py`/`.json`/`.jsonl`/`.xml`/`.toml` 落盘前先验语法，不通过则**原文件一个字不动**、连备份都不做。可用「写前语法校验」开关关掉                                            |
   | **替换歧义护栏**（新）  | `win_replace` 不带 `count` 时，若 `old` 出现多次 → 拒绝（`ambiguous_match`），要求补上下文或显式指定 `count`                                             |
   | **工具级禁用**（新）   | 逐个工具开关；被禁工具既不出现在 `tools/list`，直接调用也返回 `isError`，裸 HTTP 接口同样拒绝                                                                   |
   | **启动前拒绝裸奔**（新） | 监听地址非本机 + Token 为空/示例占位值 → **直接拒绝启动**（`--allow-unsafe` 是显式逃生阀）；Token 偏短只告警                                                      |
   | **审计日志轮转**（新）  | 默认超过 5 MB 滚动，保留 3 份历史（`audit_max_bytes` / `audit_backup_count`，填 0 = 不轮转）                                                       |
   | **暴露面收敛**（新）   | 免鉴权路径集中在 `PUBLIC_GET_PATHS`；`/health`、`/api/health` 只回 `{service, version}`，**不再回显主机名 / Python 版本 / 目录数**；`GET /` 的详细状态需要 Token |
   | **内置 HTTPS**（新）  | 填了 `tls_cert` + `tls_key` 服务就直接在端口上做 TLS；启动前先验配对，配错就明确报错退出（rc=4）而不是带着坏证书硬起                                              |
   | **自签证书生成**（新） | 没域名也能一键出证书：纯标准库（手写 ASN.1/DER + RSA 2048 PKCS#1 v1.5 确定性签名），**不依赖 openssl、不装任何包**；SAN 自动带上本机所有 IP，并有「能加载 / 能握手 / 能当信任锚校验」三重测试兜底               |
   | **中间证书链**（新）   | 证书商把「公钥证书」和「中间链」分成两个文件给（public + chain + key 三件套）时填进第三个框即可；启动时自动合并成 fullchain（站点证书在前、中间 CA 在后），落在 `wkfile\tls\fullchain.pem`；目录不可写时退回临时目录，不会因此起不来 |
   | **只读 Token**（新）  | 用只读 Token 的请求整条链路被当成只读，写/删/移/回滚一律 `read_only` 拒绝；审计里带 `scope=readonly` 便于区分                                                 |
   | **数据目录隔离**（新）  | `wkfile\`（含配置里的 Token、日志、备份、回收站）**永远不可经接口读写**，AI 读不到自己的密码                                                                        |
   | **日志全部轮转**（新）  | 审计日志与运行日志（`logs\wkfile.log`）都按大小滚动，服务长年跑不会把磁盘写满                                                                                 |
   | 并发保护           | 可选 `expected_sha256`，文件被别人改过就拒绝写入                                                                                               |
   | 鉴权锁定           | 同一 IP 5 分钟内失败 20 次即临时拒绝；Token 比对用 `hmac.compare_digest` 防时序攻击                                                                   |
   | **数据库三重闸门**（新） | 写库要同时满足：数据源开了「允许写入」+ 服务端非只读 + 用主 Token。缺哪条就明确告诉你是哪条挡的                                                            |
   | **危险语句硬拒**（新）  | 多语句、`INTO OUTFILE`、`LOAD_FILE`、`FOR UPDATE`、`SLEEP()`、`FLUSHALL`、`CONFIG`、`EVAL`、`KEYS` 等，**任何配置下都不放行**（不依赖 allow_write）              |
   | **只读会话兜底**（新）  | 只读查询会在 MySQL 会话上执行 `SET SESSION TRANSACTION READ ONLY`；就算 SQL 分类器被骗过，写入仍会被数据库自己拒绝                                    |
   | **结果限额**（新）     | MySQL 单次查询有行数上限与累计字节上限、Redis 有 key 数上限，列 key 强制走 `SCAN` —— 防止一个大请求把服务或数据库拖死                                          |
   | **凭据不回显**（新）   | 数据源列表、连接自检、审计日志里都只出现 `mysql://user@host:port/db` 这种摘要，**密码不会经任何接口回传**                                              |
   | **MCP 不写库**（新）   | 数据库配置只能从本机界面/配置文件改，没有对应的 MCP 工具 —— AI 拿不到、也改不了自己的数据库权限                                                            |
   | 审计日志           | **每次操作都记**（含 MCP 工具调用与 SQL 语句本体，v1.6.0 补上），JSONL 格式，含 IP、操作、路径、耗时、结果                                                  |
   | 客户端风险提示        | 33 个工具全部带 MCP `annotations`，客户端才能正确区分只读/破坏性操作                                                                                |
9. **仍然存在的取舍**（有意保留，不是漏做）：
   - MCP 会话是「不带 `Mcp-Session-Id` 也放行」的宽容模式，方便用 `curl` 直接调试；  
     鉴权本身不受影响（`POST /mcp` 仍然要 Token）。
   - `/health` 默认免鉴权（只回服务名和版本）。要连它也要 Token，  
     就在配置里把 `public_health` 改成 `false`。
   - 单文件读写仍有硬上限（读 4 MB / 写 16 MB / 请求体 32 MB），大文件需要分片或另行传输。
   - 改 Token / 白名单 / 端口后**必须重启服务**才生效（暂未做配置热重载）。
   - 不开 HTTPS 的话，Token 仍是明文过公网 —— 这是最大的剩余风险，见第 7 条。
10. **开数据库之前请先想清楚这三件事**（v1.6.0 新增能力）：
   - **数据库账号权限别给太高**。最省事的做法是给这个服务单独建一个只读账号
     （`GRANT SELECT ON db.* TO 'wkfile_ro'@'%'`），把「允许写入」那个开关当成第二道锁 ——
     万一配置被改、Token 泄露，数据库那一层还能兜住。真需要写就单建一个只覆盖业务表的账号，
     不要直接上 `root`。
   - **密码是明文存在 `wkfile.config.json` 里的**。它和 Token 放在同一个文件，
     而这个文件所在的数据目录已做接口隔离（AI 读不到），但**能登进服务器的人就能看到**。
     服务器有多人共用的话，记得收紧文件权限。
   - **只读判定是"尽力而为 + 数据库兜底"两层的组合**。第一层（SQL 分类器）能挡住绝大多数
     误操作和高危形态，第二层是 MySQL 的会话只读。Redis 没有等价的会话级只读，
     靠的是命令白名单 —— 所以给 Redis 开写权限时更要谨慎，`DEL` 是批量删 key 的。

---

## 8. 常见问题

**Q：客户端里 MCP 显示红色 / Failed**  
先用第 5 节的手测 `curl` 确认端点活着。401=Token 错，404=MCP 被关或路径写错，  
连不上=防火墙/安全组。另外 `url` 里别带结尾斜杠。

**Q：能连上但提示 `out_of_scope`**  
请求路径不在 `roots` 里。改配置里的 `roots`，重启服务。

**Q：中文文件名/内容乱码**  
不用管，服务端会自动识别 UTF-8 / GBK 并在读取时返回实际编码。写入默认 UTF-8。

**Q：服务器重启后服务没了**  
计划任务是 `AtStartup` + SYSTEM，正常会自动起。检查  
`Get-ScheduledTask -TaskName wkfile | Select-Object State`。  
若白名单目录在网络盘/映射盘上，SYSTEM 账户可能没权限——改普通磁盘。

**Q：公网 IP 变了怎么办**  
宽带重启会变 IP，客户端的 `url` 要跟着改。想免维护就上第 7 条第 6 点的域名 + HTTPS。  
界面上点「检测公网 IP」可以随时重新探测并回填。

---

## 9. 本机自测

```powershell
cd trae-winfile
python selftest.py                      # 老接口 + 安全机制 + MCP 协议回归：53 用例
python tests\features_e2e.py            # 功能回归（标注/轮转/护栏/预校验/工具禁用/启动防护/目录隔离/Token 有效期/HTTPS/只读 Token/备份回滚/tail/tree/批量读/**自签证书**/**证书链与三件套合并**/**数据库与缓存**/**超时边界**）：227 用例
python tests\mcp_e2e.py                 # MCP 端点（源码）：48 用例
python tests\local_e2e.py               # 本机端到端：13 用例
python tests\tls_e2e.py                 # 真起 HTTPS 服务端到端（需 openssl，无则跳过）：11 用例
python tests\mcp_exe_test.py            # MCP 端点（打包后的 exe）：22 用例
```


````

当前状态（v1.6.4 实测）：**53/53、227/227、48/48、13/13、11/11 全通过**。

重新打包 exe（改完源码后）：

```powershell
powershell -ExecutionPolicy Bypass -File .\build-exe.ps1
````

> 打包用的 Python **必须带 tkinter**（GUI 是 tkinter 写的）。python.org 的安装包有，  
> embeddable zip 没有。可用 `$env:WKFIL_BUILD_PYTHON` 指定解释器。

---

## 10. 关于作者

|  |  |
| --- | --- |
| **作者** | 吴康 |
| **微信** | `wukang` |

作者署名显示在界面**最底部独立一行**，也印在标题栏；点「⑤ 关于」页可以看到
完整说明，那里有一键复制微信号的按钮。

**本项目免费、无联网校验、无功能限制**。有问题或建议欢迎加微信沟通（备注「wkfile」）。

> 想改成你自己的名字/微信号（二次分发或内部定制），只改
> `server/wkfile_agent.py` 顶部的 `AUTHOR` / `AUTHOR_WECHAT` 两个常量，
> 界面标题、页⑤、底部署名会一起跟着变。

---

## 附录 A：为什么不用 Trae 自带的 Remote-SSH

> 本方案不依赖任何特定客户端。这一节只解释**为什么选 MCP 而不是 Remote-SSH**，
> 所以拿最常被问到的 Trae 举例。

Trae 的「远程资源管理器 / Remote-SSH」**目前不支持 Windows 目标机**：官方只支持 Linux  
（Debian 10+ / Ubuntu 20.04+），连接时会向目标机发送 **Linux shell 语法**的探测脚本，  
Windows 的 `cmd.exe` 解析不了，连接被主动断开；设置里强行写  
`remote.SSH.remotePlatform: windows` 也不生效。

所以本方案走 **MCP**：Trae / Cursor / Claude Desktop 这些客户端都原生支持 MCP，
且支持 `Streamable HTTP` / `SSE` 两种**远程**传输，  
把文件操作封装成 AI 能调用的工具，由服务器自己提供端点 —— 换客户端不用改服务端一行。

## 附录 B：已废弃方案（`deprecated\`）

以下三样曾一度是主要路线，2026-10-14 确认**用不到**，已移入 `deprecated\` 并在其中留有说明：

| 文件                          | 曾用于                 | 废弃原因                                         |
| --------------------------- | ------------------- | -------------------------------------------- |
| `隧道启动.bat`                  | SSH 隧道转发端口          | 隧道前提是 22 端口能连；而既然 22 能连，业务端口本来就能直连——隧道是多余的一层 |
| `enable-openssh-server.ps1` | 服务器装 sshd           | 同上，sshd 整条线都不需要                              |
| `client\`（本地桥接）             | 客户端侧跑 stdio 进程转协议  | 公网直连已实测全通，桥接只是保底；且主流客户端都支持 http 传输          |

详见 `deprecated\README.txt`。

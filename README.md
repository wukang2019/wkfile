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
> `initialize` → `tools/list`（36 个工具，exec 未开启时；开启后 37）→ `win_roots` → `win_list` 全部成功，  
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
├─ wkfile-v1.7.26\                  # ★★ 部署包：整个文件夹拷到服务器就能用
│   ├─ wkfile.exe                  #   主程序（免装 Python）
│   ├─ 文件夹说明.txt               #   先看这个：包里每个文件是干什么的
│   ├─ 使用说明.txt                 #   从零到跑通的全流程（纯文本）
│   ├─ README.md                   #   完整文档
│   ├─ 以管理员身份启动.bat          #   带 UAC 提权地打开配置界面
│   ├─ connect-check.py / 连接自检.bat   # 连接自检（★ 必须与 exe 同级）
│   ├─ wkfile.config.example.json  #   配置样例
│   ├─ wkfile\                     #   ★ 运行时数据目录（配置/日志/备份/回收站）
│   └─ tools\                      #   连通性诊断脚本
├─ wkfile-v1.7.26.zip               # 上面那个文件夹的同内容压缩包
│
├─ dist\
│   └─ wkfile.exe                  # 编译产物（部署包从这儿取）
├─ server\                         # ★ 源码：服务器端
│   ├─ wkfile_agent.py             #   服务主体（纯标准库，也可直接用 Python 跑）
│   ├─ wkfile_mcp_endpoint.py      #   MCP 协议层（/mcp 与 /sse，被 agent 调用）
│   ├─ wkfile_selfcert.py          #   自签证书生成器（纯标准库，手写 DER/RSA，不依赖 openssl）
│   ├─ wkfile_mysql.py             #   MySQL/MariaDB 驱动（纯标准库手写协议，不依赖 PyMySQL）
│   ├─ wkfile_redis.py             #   Redis 驱动（纯标准库 RESP，不依赖 redis-py）
│   ├─ wkfile_sqlsrv.py            #   SQL Server 驱动（纯标准库手写 TDS 协议 + TLS，不依赖 pyodbc）
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
│   ├─ features_e2e.py             #   新增能力自测（…）：195 或 211 用例，随有无 openssl 浮动
│   ├─ mcp_e2e.py                  #   MCP 端点（Streamable HTTP + SSE + 鉴权 + 越权拦截）
│   ├─ local_e2e.py                #   本机端到端（源码路线）
│   ├─ tls_e2e.py                  #   真 HTTPS 链路（需本机有 openssl，无则跳过）
│   ├─ mcp_exe_test.py             #   MCP 端点（打包后的 exe，需先 build）
│   └─ sqlsrv_proto_test.py        #   SQL Server 驱动协议层单测（152 用例，无需真实服务器）
├─ deprecated\                     # 已废弃方案归档，见里面的 README.txt
├─ .trae\mcp.json                  # Trae 项目级 MCP 配置（含真实 Token，勿提交 git）
├─ selftest.py                     # 老接口 + 安全机制 + MCP 协议自测（53 用例）
├─ build-exe.ps1                   # 重新编译 exe
├─ build-release.py                # 重新生成部署包文件夹 + zip
├─ LICENSE                         # MIT
└─ README.md
```

**部署时只需要 `wkfile-v1.7.26\` 这一个文件夹**（或者解压同名 zip），
其余的 `server\`、`dist\`、`tools\` 等都是开发用的源码与中间产物。

### 运行时只创建一个文件夹：`wkfile\`

```
wkfile\                          ← 程序启动时唯一会创建的地方（就在 exe 旁边）
├─ wkfile.config.json            配置（含 Token，别外传）
├─ wkfile.pid                    服务进程号
├─ logs\wkfile.log               运行日志
├─ logs\audit.jsonl              审计日志（超 5 MB 轮转，留 3 份；文件操作记
│                                  path，数据库操作记 source/sql/database/table）
├─ backups\<时间戳-毫秒>\<根目录>\…  改写/覆盖文件前的自动备份
│                                  （按总量 backup_max_bytes / 份数 backup_retain_count /
│                                   天龄 backup_retain_days 三维度从最旧开始清，后两项默认 0）
└─ trash\<trash_id>\…            删除的东西（可用 trash_id 恢复，不是真删；
                                   超过 trash_retain_days 天自动清，默认 30 天）
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
| **`wkfile-v1.7.26\` 整个文件夹** | **Windows 服务器** | 拷过去就行，里面含 exe + 说明书 + 自检脚本 + 工具 |

单文件绿色版：不安装、不写注册表，卸载就是删掉。**建议放 `D:\wkfile\` 这类普通目录**；  
放 `C:\Program Files\` 的话配置和日志会自动改写到 `C:\ProgramData\wkfile\`。

> 首次双击若提示「未知发布者」（没买代码签名证书，不是病毒），点  
> 「更多信息」→「仍要运行」。若被**智能应用控制（SAC）**&#x76F4;接拦死且没有「仍要运行」按钮，  
> 那台机器只能跑源码版（`启动界面.bat` + Python）—— 自编译的 exe 会被 SAC 直接拦下，  
> 不要指望「用旧版 exe 绕过」，旧版同样可能被拦，而且旧版不含最新的安全修复。

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

配好后客户端的 MCP 列表里 `wkfile` 显示 **Running**，工具列表里能看到 36 个 `win_*` 工具（默认 exec 关闭；开启后 37 个）。

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

## 6. 可用工具（33+1 个）

**文件类（23 个）**

| 工具                    | 作用                                 |
| --------------------- | ---------------------------------- |
| `win_roots`           | **先调这个**：看能操作哪些目录、是否只读、这次连接是不是只读 Token |
| `win_disk`            | 各磁盘容量/剩余空间                         |
| `win_list`            | 列目录（支持递归、通配符过滤）                    |
| `win_tree`            | 一次拿整个目录树概览（带缩进与大小），摸项目结构用          |
| `win_info`            | 单个文件/目录详情，可算 sha256                |
| `win_read`            | 读文本（带行号，可分段读大文件，自动识别 UTF-8/GBK）；`hash=true` 顺带返回全文件 sha256 |
| `win_tail`            | **只看文件末尾 N 行** —— 查日志、看最新报错，不必读整个文件  |
| `win_multi_read`      | 一次读多个文件（配置+日志+代码），单个失败不影响其它        |
| `win_write`           | 写/覆盖文本（自动建目录、写前备份、可带 sha256 防并发覆盖） |
| `win_replace`         | 精确字符串/正则替换；支持 `dry_run` 预览与 `expected_sha256` 防并发覆盖 |
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
| `win_probe`           | **连不上/行为异常先调它**：一次自检版本、配置分层、目录可达性、审计健康、备份去重、安全开关与问题清单 |
| `win_validate`        | 写前校验（不落盘）：路径白名单、乐观锁、old 命中、体积、目标语法一次验完 |
| `win_patch`           | **多文件原子补丁**：全部预检通过才写，任一失败整体回滚；支持 dry_run |

**数据库/缓存类（10 个，可选）**

| 工具                    | 作用                                 |
| --------------------- | ---------------------------------- |
| `win_db_list`         | **先调这个**：看配了哪些数据源、这次有没有写权限         |
| `win_db_test`         | 测连通性：服务端版本、认证方式、当前身份、可见的库          |
| `win_db_query`        | MySQL/MariaDB/SQL Server **只读**查询（SELECT/SHOW/DESC/EXPLAIN/WITH） |
| `win_db_exec`         | MySQL/MariaDB/SQL Server **写**语句（需要数据源开启允许写入）  |
| `win_db_tables`       | 列库中的表（引擎/架构、估算行数、占用空间）            |
| `win_db_schema`       | 看某张表的完整列定义与索引                      |
| `win_redis_info`      | Redis 概况：版本、连接数、内存、各库 key 数        |
| `win_redis_keys`      | 按模式列 key（走 `SCAN`，**不会**像 `KEYS` 那样阻塞实例） |
| `win_redis_get`       | 读单个 key，按实际类型自动展开（string/hash/list/set/zset/stream） |
| `win_redis_write`     | 执行 Redis 写命令（需要数据源开启允许写入）           |

**运维类（1 个，默认关闭）**

| 工具                    | 作用                                 |
| --------------------- | ---------------------------------- |
| `win_exec`            | **远程执行命令**（composer / php / 清缓存）。默认关闭：界面②勾选「执行远程命令」+ 配置程序白名单 |

> **每个工具都带了 MCP 标准风险标注**（`readOnlyHint` / `destructiveHint` /  
> `idempotentHint` / `openWorldHint`）。这不只是文档：MCP 规范里"不标注"的默认值是  
> **破坏性 + 面向开放世界**，也就是说只读工具会被客户端当成危险操作来提示。  
> 现在 21 个只读、13 个写入各自标注准确，客户端那边的安全提示才是对的。

> **备份与回收站的区别**：`win_trash` 是"我要删掉它"，进回收站；  
> `win_backup_*` 是"它被改过，我要看以前的版本"，读的是 `wkfile\backups\`。  
> 两者互不相干，回滚前也会先把当前位置再备份一份，所以**回滚本身也可撤销**。
>
> **备份失败会整体中止操作**：覆盖 / 替换 / 移动 / 回滚前若自动备份失败
> （磁盘满、权限不足等），该次操作直接报错、原文件与目标文件都不改动，
> 不会留下「改坏了却没备份可回滚」的裸奔状态。

> **所有 MCP 工具调用都写审计日志**（含只读 Token 请求的 `scope=readonly` 标记）。

> **`win_replace` 有一条护栏**：不带 `count` 时，如果 `old` 在文件里出现多次，
> 本次调用会被**拒绝**（`ambiguous_match`），要求补更长的唯一上下文或显式指定 `count`。
> 目的是避免"只想改一处 `localhost`，结果文件里 4 处全被改掉"这类不可逆误伤。
> 未命中时返回里带 `file_head`（文件开头 200 字符），省得再 read 一趟排查。

> **`win_exec`（远程命令执行）七重围栏**——它是全服务权限最高的能力，默认整体关闭。
> **先说结论：这七重是给可信运维操作者减误伤的静态检查，不是安全边界。**
> 一旦开启 `win_exec`，拿到它的操作者就应当被视为能在这台机器上执行任意
> 命令；不要拿它当沙箱去隔离不可信的 AI 或不可信用户。
> ① `exec_enabled` 总开关（不开 = tools/list 里根本没有这个工具）；
> ② 程序白名单 `exec_allowed_commands`：v1.7.8 起**只查整条命令的首程序**
> （剥掉 `cmd /c` 前缀后第一个 token，`php` 等价 `php.exe`、全路径调用也认），
> 后面的参数、`&&`、`|` 管道、`2>&1` 重定向、带引号的字符串都原样交给 cmd
> 自己处理——实测逐段拆链会让复合命令寸步难行。想回到逐段严格模式（命令链
> 每一段的首程序都过名单，防 `php a.php && del /s .` 借道），配
> `exec_strict_chain = true`；
> ③ 工作目录限定在授权根目录内；
> ④ 超时硬顶（默认 60s，上限 300s），超时连子进程树一起杀；
> ⑤ 只读 Token / 只读模式拒绝；
> ⑥ 审计日志记录完整命令，输出截 64KB；
> ⑦ **cwd 劫持防护（尽力而为的静态检查）**——Windows 下 cmd 执行不带路径
> 的程序时搜索序是 **cwd 优先于 PATH**，而 roots 恰恰是 AI 可写目录：往里
> 面放一个与白名单程序同名的 `php.bat`，`php xxx` 实际执行的就是投放文件。
> 目前会查：拆链后能静态定位的程序位（含 `call` / `start` / `cmd /c` 转发
> 前缀、换行也参与拆链）、含变量或转义的程序名一律拒绝、带路径的脚本引用
> （`%cd%\x.bat`、`.\x.bat`、`sub\x.bat`）、cwd 里的同名可执行变体
> （.bat/.cmd/.exe/.com/.ps1/.vbs），以及 `shutil.which` 按 PATH 解析到
> roots 内的程序。**无路径的纯文件名参数不查**（`type deploy.ps1`、`copy payload.bat x`
> 里 deploy.ps1 / payload.bat 只是读/复制的目标，不带 `.\` 或子目录前缀，不会被
> 当成投放脚本），避免误伤；但**带相对路径的 `type .\deploy.ps1`、`copy .\payload.bat x`
> 会落到这条「带路径脚本引用」检查**（见下方白名单与围栏⑦的关系）。
>
> **已知限制（v1.7.24 起不再继续加固围栏）**——下面这些写法当前**不会被
> 拦截**，围栏不承诺覆盖它们：
> - `dir && if exist payload.bat payload.bat`
> - `dir && (payload.bat)`
> - `dir && >nul payload.bat`
>
> 前提都是 `exec_enabled=true`（默认关闭）。根因不是某一处漏写判断，而是
> 方法本身有天花板：cmd 的 `if` / `for` / 括号 / 重定向 / 变量展开可以自由
> 组合，用正则或词法去判定一段自由文本「会不会执行某个文件」永远堵不完。
> 这条线上已经往返四次（堵死 → 误伤 → 收窄 → 又漏），继续打补丁只会让
> 误伤和漏拦来回摆动。要真正的安全边界得换设计：**typed 命令 + 参数
> schema + 服务端用绝对路径直接调用解释器**（不再接受自由命令行）——本版
> 不做这个改造。在那之前：默认保持 `exec_enabled=false`；确需开启时只给
> 可信操作者，白名单只放必要程序，别放 `powershell`。
>
> v1.7.9 补强：
> * **stdin 参数**——要跑一段 PHP/Python 脚本，把整段脚本放 `stdin` 直喂，
>   不用再拼 `php -r "引号嵌套地狱"`；
> * **白名单热加载**——改配置文件里的 `exec_allowed_commands` 保存即可，
>   不用重启服务（`exec_enabled` 总开关仍需重启）；
> * **超长输出落盘**——超过上限的完整输出存到数据目录 `logs\exec-*.txt`
>   （保留最近 30 份），返回 `output_file` 路径，`win_read` 可直读全量；
> * **structuredContent**——`exit_code` / `duration_ms` 等机器可读字段
>   按新版 MCP 规范随结果返回，AI 不用再从文本头部解析。
>
> **推荐白名单起步**（无破坏力：`php`、`php.exe`、`composer`、`composer.phar`、
> `cmd`、`dir`、`type`、`where`、`findstr`、`tasklist`、`netstat`、`curl`）。
> 谨慎追加：`del`/`rd`（不可逆删除）、`move`/`copy`（搬运覆盖）、`net`/`sc`/`wmic`
> （服务管理）、`certutil`（常被滥用为下载器）；**`powershell` 几乎等于全开**
> （`-Command` 能绕过一切白名单），`exec_strict_chain` 也挡不住它——加进去
> 就等于把任意命令执行能力完整交出去，别指望围栏兜底。
>
> 开启入口：界面②「安全与权限」→「远程命令执行」区，或直接改
> `wkfile.config.json` 的 `exec_enabled` + `exec_allowed_commands` 后重启。
>
> **cmd 内建命令（type / copy / del / move / where / findstr 等）也要写进白名单**
> 才会被放行：白名单只认「整条命令的首程序」，无路径的 `type a.txt`、`copy a b`
> 这类命令首程序是 `type` / `copy`，不在白名单里会被拦 —— 想用就把 `type` / `copy`
> 加进白名单。**但带相对路径引用的写法（`type .\deploy.ps1`、`copy .\payload.bat x`）
> 即使 `type` / `copy` 已在白名单里也照样会被拦，而且拦它的不是白名单、是上面的
> 围栏⑦ cwd 劫持防护**：护栏把 `.\x.ps1` / `sub\x.bat` 这类「cwd 优先于 PATH 的脚本
> 引用」当潜在劫持处理，只要工作目录里真有同名脚本就拒绝执行（错误码
> `exec_hijack_suspected`）。给 `type` / `copy` 加白名单**绕不过它**——这是常见
> 排查坑：按「加白名单就能放行」去改配置是白费功夫。真要读/复制 cwd 里的这类文件，
> 用绝对路径（`type C:\abs\deploy.ps1`）或走文件 API（win_read / win_copy），别在
> cwd 内用 `.\` 引用脚本。推荐白名单里已经列了 `type`、`where`、`findstr`，按需自行
> 补 `copy` / `del` 等。

> **本地 → 远程传大文件：`POST /api/upload`（v1.7.9+）**——几 MB 的文件
> 不再需要 base64 塞进 JSON（会超限/被截断）。请求体就是文件原始字节，
> 目标路径放查询串，带 sha256 强校验与写前备份：
>
> ```bat
> :: Win10+ 自带 curl
> curl -X POST --data-binary @xxx.php -H "Authorization: Bearer <主Token>" ^
>      "http://服务器:8788/api/upload?path=E%%3A%%5Ckanghui%%5Capp%%5Cxxx.php"
>
> :: 或用部署包里的脚本（自动算 sha256）
> python tools\upload.py --url http://服务器:8788 --token <主Token> ^
>                         --from d:\deving\kh\app\xxx.php --to E:\kanghui\app\xxx.php
> ```

> **为什么大文件别走 `win_write_b64`（v1.7.10 起工具描述会明确提示）**——
> base64 让内容膨胀 33%，超长参数会被 AI 客户端**静默截断**：截断发生在
> 客户端，服务端收到的仍是合法 base64，自己发现不了，残缺文件直接上盘
> （典型症状：写完 PHP 报 Fatal）。两个兜底：
> ① 传 `expected_sha256`（按**解码后原始内容**计算）——不匹配 409 拒写，
>   错误信息会引导改走 `/api/upload`；
> ② 文件超过 ~8KB 一律走上面的 `/api/upload` 直传，根本不进 base64。

### 数据库工具怎么开

默认**一个数据源都不配，数据库能力就是完全关闭的**。要开启：启动界面 → **④ 数据库** →
「＋ MySQL」「＋ SQL Server」或「＋ Redis」→ 填地址账号 → **「测试连接」** → 「保存配置」→ 重启服务。

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
//      "max_keys":200,"timeout_sec":15,"connect_timeout_sec":8},
//     {"name":"erp","type":"sqlserver","host":"127.0.0.1","port":1433,
//      "user":"sa","password":"","database":"erp","allow_write":false,
//      "max_rows":200,"timeout_sec":30,"connect_timeout_sec":8}
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

**语句分级**：

| 级别 | 判定 | 处理 |
| --- | --- | --- |
| 只读 | `SELECT` / `SHOW` / `DESC` / `EXPLAIN` / `WITH...SELECT`（SQL Server：`SELECT` / `WITH...SELECT` / `USE`） | 直接放行 |
| 写 | `INSERT` / `UPDATE` / `DELETE` / DDL / `SET` 等 | 需三重闸门全开 |
| 拒绝 | 多语句、`INTO OUTFILE`、`LOAD_FILE`、`FOR UPDATE`、`SLEEP()`、`xp_cmdshell`、`OPENROWSET`、`SELECT...INTO` 等 | 任何情况都不放行 |

只读判定的实现细节值得说明：判定前会先**剥离注释与字符串字面量**，所以
`SELECT 'drop table x'` 不会被误判成删表，`/* x */ DELETE` 也不会被漏掉。
另外只读查询还会在会话上执行 `SET SESSION TRANSACTION READ ONLY` —— 让**数据库自己**
兜底，就算分类器被骗过，写入依然会被 MySQL 拒绝。
（SQL Server 没有会话级只读开关，写拦截完全靠 T-SQL 分类器 + 三重闸门；
这一点在界面的提示里也写明了。）

**Redis 侧用白名单**：只有明确列在只读清单里的命令才放行，其余一律按写处理，
`FLUSHALL` / `FLUSHDB` / `CONFIG` / `EVAL` / `MONITOR` / `SHUTDOWN` /
`SUBSCRIBE`（会改变连接应答模型）等永远拒绝。列 key 强制走 `SCAN`，
**`KEYS` 被禁用**——它会阻塞整个实例。

> 驱动是**纯标准库手写**的（`wkfile_mysql.py` / `wkfile_redis.py` / `wkfile_sqlsrv.py`），
> 依然不需要 `pip install`，也不依赖 `mysql.exe` / `redis-cli` / `sqlcmd`。
> MySQL 8 默认的 `caching_sha2_password`（含首次连接的 RSA 公钥交换）已完整支持，
> MariaDB / MySQL 5.7 / Redis 3.x 以上都验证过。
>
> **SQL Server 驱动（v1.7.0 新增，v1.7.2-1.7.5 修正协议细节，v1.7.6 增强数据保险链）**：自己实现的 TDS 7.4 协议（prelogin 加密协商 →
> TLS 握手（握手报文按协议要求包在 TDS 包里搬运）→ LOGIN7 SQL 身份验证 →
> token 流解析），支持 NBCROW 空值压缩行、varchar(max) 分块大对象、
> DECIMAL 精确十进制、DATETIME2/DATETIMEOFFSET 等新版类型。边界：
> 仅 SQL 身份验证（sa 等账号，不支持 Windows 集成认证）、仅全链路加密
> （默认自签名证书即可）、需 SQL Server 2008+；协议层有 152 个单测
> （`tests\sqlsrv_proto_test.py`，无需真实服务器即可跑）。

**两个超时是两个概念，别混**（界面④里是两个独立的框）：

| 字段 | 含义 | 默认 | 什么时候要调 |
| --- | --- | --- | --- |
| `connect_timeout_sec` | **拨号预算**：域名解析 + 逐个地址 TCP 连接，总共就这么多秒 | 8 | 库在跨地域/公网、建连慢，或想让它更快失败 |
| `timeout_sec` | **查询超时**：连上之后等结果的时间 | MySQL / SQL Server 30，Redis 15 | 有大表慢查询，或想限制单次调用时长 |

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
   | 并发保护           | 可选 `expected_sha256`（`win_write` / `win_replace`），文件被别人改过就拒绝写入，并在错误里回传当前 sha256 / 大小 / 修改时间                                        |
   | 鉴权锁定           | 同一 IP 5 分钟内失败 20 次即临时拒绝；Token 比对用 `hmac.compare_digest` 防时序攻击                                                                   |
   | **数据库三重闸门**（新） | 写库要同时满足：数据源开了「允许写入」+ 服务端非只读 + 用主 Token。缺哪条就明确告诉你是哪条挡的                                                            |
   | **危险语句硬拒**（新）  | 多语句、`INTO OUTFILE`、`LOAD_FILE`、`FOR UPDATE`、`SLEEP()`、`FLUSHALL`、`CONFIG`、`EVAL`、`KEYS`、`xp_cmdshell`、`OPENROWSET`、`SELECT...INTO` 等，**任何配置下都不放行**（不依赖 allow_write）  |
   | **只读会话兜底**（新）  | 只读查询会在 MySQL 会话上执行 `SET SESSION TRANSACTION READ ONLY`；就算 SQL 分类器被骗过，写入仍会被数据库自己拒绝（SQL Server 无此开关，靠 T-SQL 分类器 + 三重闸门兜底）          |
   | **结果限额**（新）     | MySQL / SQL Server 单次查询有行数上限与累计字节上限、Redis 有 key 数上限，列 key 强制走 `SCAN` —— 防止一个大请求把服务或数据库拖死                                 |
   | **凭据不回显**（新）   | 数据源列表、连接自检、审计日志里都只出现 `mysql://`、`sqlserver://` 这种摘要，**密码不会经任何接口回传**                                              |
   | **MCP 不写库**（新）   | 数据库配置只能从本机界面/配置文件改，没有对应的 MCP 工具 —— AI 拿不到、也改不了自己的数据库权限                                                            |
   | 审计日志           | **每次操作都记**（含 MCP 工具调用与 SQL 语句本体，v1.6.0 补上），JSONL 格式，含 IP、操作、路径、耗时、结果                                                  |
   | 客户端风险提示        | 37 个工具全部带 MCP `annotations`，客户端才能正确区分只读/破坏性操作                                                                                |
9. **仍然存在的取舍**（有意保留，不是漏做）：
   - MCP 会话是「不带 `Mcp-Session-Id` 也放行」的宽容模式，方便用 `curl` 直接调试；  
     鉴权本身不受影响（`POST /mcp` 仍然要 Token）。
   - `/health` 默认免鉴权（只回服务名和版本）。要连它也要 Token，  
     就在配置里把 `public_health` 改成 `false`。
   - 单文件读写仍有硬上限（读 4 MB / 写与请求体统一 16 MB），大文件走 /api/upload 直传或分片。
   - GUI 与服务是**两个独立进程**（同一个 exe 文件）：关掉界面不会停掉已启动的服务，
     服务卡住也不会冻结界面。「刷新状态」「检查端口」的探测在后台线程跑，连点会合并成
     一次，所以点完状态栏可能过几百毫秒才变，不是没生效（v1.7.14）。
   - 页①的「运行状态」区显示服务状态、监听地址、PID、近 1 小时请求/失败/失败鉴权数
     与最近 3 条审计，数据只读本机审计日志与 PID 文件，**按需刷新、不自动轮询**。
     「精简弹窗」默认开：成功/信息/一般警告只进状态条与日志，错误和破坏性确认仍弹窗
     （v1.7.15）。
   - MCP 响应里的大正文只发一份：`content` 文本块已经是全文，`structuredContent`
     在正文超过 4 KB 时会省略重复的 `content`，改给 `content_omitted` /
     `content_bytes`；小响应原样保留，旧客户端不受影响（v1.7.13）。
   - **AI 该知道的约定在握手时就下发**（v1.7.16）：`initialize` 的 `instructions`
     除入门流程外，还带一段按**当前配置实时生成**的「运行时契约」——版本与本次
     Token 权限、读/写上限、`/api/upload` 引导阈值、`structuredContent` 去重阈值、
     `win_read` 续读与残页拼接、备份失败整体中止、`utf-8-sig` 禁用、`win_exec`
     开关与白名单。数字全部取自代码常量，不会随文档漂移；**没有新增工具、
     没有新增调用、包里也没有多一份机读文档**。改了配置重启后，AI 下次连接
     看到的就是新值。
   - **`win_read` 拼接只看事实字段**（v1.7.17）：每页返回
     `content_ends_with_newline`，表示该页末行在原文里是否由换行终止；仅当它为
     `true` 时页尾补一个 `\n`，否则直接接下一页。**不要按 `line_fragment` / `eof`
     猜**——末尾有无换行的文件会各错一半。同时修好「末行无换行且跨页」时 EOF 页
     少算一行的问题，各页 `returned_lines` 之和恒等于逻辑行数。
   - **命令输出不再丢尾部报错、数据库翻页不用猜、备份清得掉**（v1.7.18）：
     `win_exec` 超限时只回「头 20 行 + 尾 40 行」压缩预览（`head_lines` /
     `tail_lines` / `max_output_bytes` 可调小省 token，后者**不能超过**服务端上限），
     全文落在 `output_file`，`win_tail` 现在能直接读它看尾部报错（旧版被
     「数据目录禁访」拒绝，只能拿 `win_read` 逐页翻）。
     `win_db_query` 截断时返回 `page_hint`：该数据源方言的分页 SQL 模板、下一页
     `OFFSET` 和稳定 `ORDER BY` 提醒。**没有加 `offset` 参数** —— MySQL / SQL Server
     都是读完整结果流再丢弃超出行，API 层 offset 只改丢弃区间，数据库一点没省，
     还会因两次查询间数据变动重复/漏行；按模板改写 SQL 才是真分页。
     备份保留新增 `backup_retain_days` / `backup_retain_count` 两个服务端配置，
     与原有 `backup_max_bytes` 三者任一超限就从最旧的时间戳目录清。**默认都是 0**
     （升级后不擅自删已有备份，需显式启用）：攒了几百份小文件就填
     `backup_retain_days=30`、`backup_retain_count=200`。`win_backup_list` 返回只读
     `policy` 快照（总份数 / 总体积 / 按策略会清几份），**不提供删除备份的工具**
     —— 让 AI 能删掉回滚兜底比写错文件更危险，手动清理请由运维在服务器上做。
   - **写类工具备份口径统一、命令超时上限可配**（v1.7.19）：
     `win_write` / `win_write_b64` / `win_replace` / `win_append` 现在**都**支持
     `backup=false` 按次跳过写前备份（此前只有前两个支持，`win_append` 每追加
     一行都要把几 MB 的日志文件整份复制一遍）。`win_exec` 的超时天花板不再是
     写死的 300 秒，新增服务端配置 `exec_timeout_max_sec`（默认 300 = 保持旧
     行为，夹到 1~3600 秒，改完保存配置下次调用即生效）；按调用传的 `timeout`
     只能调小到这个上限之内。**仍然不做异步 exec**：长任务请在工具描述与运行时
     契约的提示下拆成多次短命令，不要靠拉长超时硬扛。
   - **并发覆盖保护的闭环补齐**（v1.7.20）：`win_write` 原先描述让 AI「把
     `win_read` 返回的 sha256 传给 `expected_sha256`」，但 `win_read` 从来不返回
     sha256 —— 照做必然拿不到值。现在 `win_read(hash=true)` 一次调用同时给内容和
     **全文件** sha256（默认不开、零开销；文件过大时降级说明原因而不中断读取），
     纯文本客户端的文本头里也能看到。`expected_sha256` 冲突时错误里直接回传
     **当前** sha256 / 大小 / 修改时间和重试指引，AI 不用再跑一趟 `win_info` 探测；
     `win_replace` 也补上了 `expected_sha256`（`dry_run` 与未命中路径同样校验），
     防止读取与替换之间别人的改动被静默覆盖。乐观锁比对统一走带体积上限的算法，
     超大文件明确拒绝写入而不是硬算哈希或静默跳过校验。
   - **五项剩余待办一次收口**（v1.7.21）：
     - **配置分层**：内置默认 < `wkfile.config.json` < 环境变量
       `WKFILE_<键名大写>`（例：`WKFILE_PORT=9000`）。只允许覆盖已有键，
       拼错/脏值忽略并在 `win_probe` 报告；环境变量**不会**被界面保存写回
       文件，也不会被热加载冲掉（撤掉环境变量即恢复文件里的值）。
     - **纯逻辑外提**：新增 `server/wkfile_core.py`，收拢编码/哈希/Token
       有效期/超时夹取/配置合并/补丁预检等无状态函数；`wkfile_agent` 保持
       旧名字导出，既有脚本与测试零改动，构建脚本补 `--hidden-import`。
     - **审计降级**：审计盘写不进去时最近 N 条进内存缓冲（默认 200），
       恢复后按原顺序自动补写；主操作绝不因账本失败而失败，`win_probe` 报出
       降级状态与缓冲/丢弃/补写计数。
     - **增量备份**：内容相同的历史版本用 NTFS 硬链接共用一份磁盘实体
       （`backups/.objects/`），每个版本仍是**可直接读取与回滚的完整文件**；
       不支持硬链接或超过 `backup_dedup_max_bytes`（默认 64 MB）时自动整份
       复制，`backup_dedup=false` 可完全关掉。
     - **三个新工具**：`win_validate` 写前校验（不落盘，只读 Token 也能调）、
       `win_patch` 多文件原子补丁（全部预检通过才写，中途失败整体回滚）、
       `win_probe` 一次自检出版本/配置分层/目录可达性/审计健康/备份去重/
       安全开关与问题清单（不输出 Token）。
     - **本版替运维拍板的假设**（与实际场景不符改配置即可，无需改代码）：
       增量备份默认开且用硬链接而非 diff 链，回滚语义零变化；
       `win_patch` 始终原子、不提供「允许部分成功」开关，单次最多 20 个
       文件 / 合计 8 MB；审计缓冲 200 条；环境变量只认已有键。
       **仍明确不做**：异步 `win_exec`、`win_db_query offset`、开放给 AI
       删除备份（让 AI 删掉回滚兜底比写错文件更危险）。
   - **响应契约与两处安全补漏**（v1.7.22，外部体检发现）：
     - **P0：`win_patch` 写成功却回不了响应**。旧版把原文件的**完整字节**
       塞进 `changes[*].original`，HTTP 侧 `json.dumps` 直接 500、MCP 侧
       连接断；可文件已经落盘、审计也已经记了成功 —— 客户端看到失败就
       重试，同一处补丁被重复应用到已改过的文件上。现在只回
       `original_sha256` + `original_bytes`，回滚用的原字节改成内部字段
       （下划线开头，构造响应时剔除）；`win_patch` 描述、`rollback_hint`
       与文本渲染都明确要求：重试前先用 `win_read(hash=true)` 核对，或
       下一次调用带 `expected_sha256` 让服务端拦下重复应用。
     - **围栏⑦补漏**：旧版每段只查首程序，`call` / `start` 被当内建整段
       跳过，`dir && call payload.bat` 仍能执行 cwd 里的投放脚本。现在会
       剥离 `call` / `start` / `cmd /c` 转发前缀继续查真实程序，并对段内
       **任意位置**引用了 cwd 已存在脚本的写法一律拒绝（覆盖
       `%cd%\x.bat`、`.\x.bat`、`sub\x.bat`、`cmd /c "x.bat"` 等变体）。
     - **回滚体积闸**：原子补丁原先无上限地整份读入原文件，>4 MB 的目标
       就能让请求线程同时扛原文 + 新内容 + 响应。新增
       `patch_max_rollback_bytes`（默认 8 MB，同时管单文件与单次总量），
       在动第一个文件**之前**结清预算，超限明确拒绝、磁盘一字不动。
     - **契约护栏**：新增 `tests/v1722_test.py`（105 项），把「响应必须能
       `json.dumps`」覆盖到全部 37 个 MCP 工具与全部 HTTP action；同时清掉
       旧测试里的 `check(..., True)` 恒真断言、「分页没发生却记 PASS」的
       假绿，并在 MCP 结果上补序列化断言。**「断言绿但契约错」不再有第三次**。
   - **围栏 / 备份 / 文档一轮推平**（v1.7.23，第二轮外部体检）：
     - **围栏⑦再补**：`set X=p.bat` 换行 `call %X%`、`for %i in (...) do
       call %i` 把程序名藏进变量，字面量检测必然看不到。现在换行也参与
       拆链，程序位置含 `%` / `!` / `^` 一律拒绝（`exec_dynamic_program`）。
       反向误伤也修掉：旧版把 `type deploy.ps1`、`copy payload.bat x` 的
       **参数**当投放脚本拦下，现在段内兜底只查「程序位置」和「带路径
       的脚本引用」。
     - **体积闸脚枪**：`patch_max_rollback_bytes=0` 旧版等于关掉上限、
       退回无边界 `fh.read()`。现在 0 / 负数一律回退内置 8 MB，并在
       `win_probe` 报配置异常；示例配置注释也不再写「0 = 不限制」。
     - **四条老账**：① 备份体积按 inode 去重，硬链接不再被算成 N 份而
       误删最旧版本（`physical` / `logical_bytes` 分开报）；② 新增服务端
       `backup_required`，开启后 AI 传 `backup=false`、或配置把
       `auto_backup` 关掉，都直接拒写；③ 审计 `dropped>0` 有独立的
       `integrity` / `alerts`，`win_probe` 与 MCP 文本明确告警，不再只
       暴露一个数字；④ 只读 Token 调 `win_probe` 时 roots / 备份绝对路径
       脱敏（`win_roots` 不受影响，它是 AI 构造路径所必需）。
     - **文档**：features 用例数 207 / 208 / 211 三处统一为 211，命令块
       补 `v1722_test.py` / `v1723_test.py`；`使用说明.txt` 删掉「投放脚本
       再也借不到道」的超卖口径；修掉 `build-release.py` 的 `newline`
       双重转换 —— 发布包 .txt / .bat 里 509 处 `\r\r\n` 消失。
     - 新增 `tests/v1723_test.py`（59 项），同样先写「修复前应该红」的
       断言：修复前 33 项红，逐条对应本轮问题。
   - **备份超删与围栏口径**（v1.7.24，第三轮外部体检）：
     - **备份清理超删（必修）**：开了硬链接去重后，`.objects` 会留下孤儿
       （`os.link` 失败是代码自己承认的正常状态）。旧版启动清理不 prune
       孤儿，却把它的字节算进 `physical`；删版本永远减不掉这部分，于是
       `backup_max_bytes` 会一路删到把有效备份全部清空、孤儿还在 —— 安全网
       静默消失，直到真要回滚才发现。现在清理前先 prune；即使 prune 因权限
       或竞态失败，统计也会把孤儿从 `physical` 剔除，清理计划另加一道
       「不可回收体积」护栏，且体积维度永不删掉最后一份备份。
     - **`win_backup_list` 不再虚报**：`would_remove` 旧版漏传 `physical`，
       孤儿场景下报 0、实际删 2 份。现在与 `cleanup_backups` 同一个口径；
       `orphan_objects` / `orphan_bytes` / `unreclaimable_bytes` 如实报出，
       文本渲染也会提示有孤儿待回收。
     - **围栏停止加固，改写口径**：`if exist payload.bat payload.bat`、
       `(payload.bat)`、`>nul payload.bat` 这些写法把当时「拆链后逐段拦截」
       的口径重新打穿了。cmd 语法无法静态穷举，本版**不改围栏逻辑**，改为在 README、
       工具说明、运行时契约与源码注释里统一写明「静态检查，不是安全边界」，
       并逐条列出已知限制与 typed 命令的改造方向。三条绕过在
       `tests/v1724_test.py` 里作为已知限制登记，不再当作待修漏洞。
     - 新增 `tests/v1724_test.py`（47 项），同样先写「修复前应该红」的
       断言：修复前 21 项红，逐条对应本轮问题。
   - **脏数值配置不再把服务/自检弄死**（v1.7.25，第四轮外部体检）：
     - 配置文件层此前**完全没有类型校验**（只有环境变量层有）。写成
       `"backup_max_bytes": "2GB"` 或 `"patch_max_rollback_bytes": "8MB"`
       这种很自然的带单位写法，会一直活到运行时的 `int()` 才炸：启动清理
       被 `try/except` 静默吞掉（保留策略整个失效、没人知道），`op_probe`
       与 `op_patch` 直接回 500 —— 连唯一能自检的工具都哑了。而手改 JSON
       的恰恰是运维本人，一次手滑就中，且在换版重启那一刻才暴露。
     - 现在加载时按 `DEFAULT_CONFIG` 的类型做一次**配置类型校验**：脏值
       回退内置默认值并记结构化告警（只有键名与类型名，不含任何配置值），
       `/api/health` 免鉴权报出数量与键名，`win_probe` 的 `issues` 与
       `config.layers.config_warnings` 给详情；自动生成的配置模板也补了
       「数值键填纯整数、不支持 2GB / 8MB」的注释。只校验类型，不改语义：
       0 / 负数 / 空串 / 空容器都照旧合法。
     - **文档两处纠错**：`features_e2e.py` 的用例数随本机有无 `openssl`
       浮动（195 或 211），旧版写死 211 还盖「实测」章，现按两种环境如实
       说明；《使用说明.txt》里「说明书里把已知不拦的写法逐条列了出来」
       是自指错误 —— 那份清单只在随包的 `README.md` 里，现明确指向。
     - 新增 `tests/v1725_test.py`，同样先写「修复前应该红」的断言，并真起
       一个带脏配置的临时服务复现换版重启场景（`/api/health` 200 含告警、
       `/api/probe` 仍 200）。
   - **手改配置不会再静默打开安全门**（v1.7.26，第五轮外部体检）：
     - v1.7.25 的「脏值回退默认值」对大小/天数/端口是对的，但**默认值对
       安全开关来说等于关掉保护**。实测四种手滑：
       `"read_only": "true"` → `False`（只读被静默关闭）、
       `"ip_allowlist": "10.0.0.0/8"` → `[]`（放行所有 IP）、
       `"allow_delete": "false"` → `True`（删除被打开）、
       `"backup_required": "true"` → `False`（强制备份失效）。
       一个引号就能把四道门同时翻到危险侧，服务照常启动、横幅一声不吭。
       比起不来更坏：**起不来能立刻发现，静默放开不会发现**。
     - 本版把两类值分开：**安全开关**（`read_only` / `allow_delete` /
       `allow_move` / `auto_backup` / `backup_required` / `exec_enabled` /
       `exec_strict_chain` / `enable_mcp` / `preflight_check` /
       `public_health`，以及数组 `ip_allowlist` / `tools_disable`）
       只严格识别 `true/false`、`1/0` 与对应的字符串写法，认不出来、
       写成 `null`、或把数组写成单个字符串就**直接拒绝启动**（退出码 5，
       不监听端口、不改写配置文件），绝不猜默认值；带引号的 `"true"` 会按意图解析成 `true`
       并留一条规范化提示。**普通数值**（大小 / 天数 / 端口）仍是回退默认
       值 + 告警，方向不变。环境变量层的安全开关脏值同样拒绝启动，
       不再只被「忽略并告警」。
     - **所有 `config_warnings` 打到启动横幅**，不再只躺在 `/api/health`
       和 `win_probe` 里；`port: "10087"` 回退成 8788 时额外提示
       「按上面 listening 行的端口连」，别照着配置里写的端口连然后以为
       服务没起来。告警只带键名与类型名，绝不回显配置值。
     - 顺带堵上同源脚枪：GUI 保存与 `--token-days` 写出的
       `token_ttl_days` 是浮点，v1.7.25 按整数校验会把 `30.0` 判脏回退
       `0`（Token 悄悄变成永久有效）。该键的默认值改为浮点，整数与浮点
       都收。GUI 遇到脏安全开关时会弹窗并锁住【保存配置】/【启动服务】，
       避免用默认值把用户原本收紧的开关覆盖掉。
     - 新增 `tests/v1726_test.py`：四个审计用例逐一验（要么按意图生效、
       要么拒启，绝不翻到危险侧），真起进程验证退出码 5 / 不绑端口 /
       不改写文件，以及脏数值配置的横幅告警与 `/api/health`。
   - **热加载与连通性配置的几处 fail-open / 静默退化（v1.7.27，第六轮外部体检）**：
     - **exec 热加载不再静默套用非法值**。此前 `_hotload_exec_cfg` 直接把
       配置文件里的值 `setattr` 上去，于是 `exec_strict_chain: null` 会被
       `op_exec` 当成 `False` 把严格链**静默关掉**（安全降级），
       `exec_allowed_commands` 写成数字会让 `for c in ...` 抛 `TypeError`
       导致 `/api/exec` 直接 500。现在每个热加载键都过类型守门：
       非法值丢弃并保留上次生效值，同时在运行日志告警。
     - **`patch_max_total_bytes=0` 不再静默全拒**。旧版把 0 当硬上限，
       于是「本次写入合计 N 字节超过预算 0 字节」把每一次补丁都拒掉、且无告警。
       现在 `0 / 负数` 视为「不限制」（与 `backup_max_bytes=0` 同一约定）。
     - **`port` / `host` 的 null / 类型不对不再静默回退默认值**。`port` 是
       连通性关键键，脏值（null / 字符串 / 越界）直接**拒绝启动**（拒绝连
       错端口还以为服务没起）；`host` 脏值回退 `0.0.0.0` 并在启动横幅告警。
     - **备份清理少走一遍 `.objects` 遍历**。孤儿实体回收直接复用
       `_backup_entries` 已经枚举出来的路径清单，不再单独再扫一次目录。
     - **文档与测试**：`type` / `copy` 等 cmd 内建命令需在 `exec_allowed_commands`
       白名单里才放行，已在 README 写明（这是白名单机制本身，不是围栏漏判）；
       《使用说明》补上 `backup_required` 等备份相关配置键的说明；
       `tests/v1723_test.py` 的 `test_docs` 从「形状断言」补成「内容断言」。
     - 新增 `tests/v1727_test.py`：热加载非法值丢弃不套用、`patch_max_total_bytes=0`
       视为不限制、port 脏值拒启、备份清理单遍遍历共四类。
   - **第七轮外部体检补刀（v1.7.28）—— 上版"修对但发太快"的两类问题**：
     - **配置热加载的"认得出但类型不对"仍会静默反方向**：v1.7.27 只"丢弃认不出
       的值"，于是 `exec_strict_chain` 写 `0` 被 `coerce_safety_bool` 当成 `False`
       **静默关掉严格链**、写 `"false"` 被当成真值字符串 **静默开链**（比 `null`
       更隐蔽，`null` 至少长得像错）。现在热加载一律先 `coerce_safety_bool`
       **规范化后再写布尔值**，`0`/`"false"`→`False`、`1`/`"true"`→`True`，且规范化
       动作本身要打告警——不再把裸值 `setattr` 上去。
     - **告警不能只躺在 run log 里**：热加载对非法值（跳过）与需规范化的值（已按
       布尔处理）的处理，现在计入 `Agent._hotload_warnings`，并同时出现在**需鉴权的
       `win_probe`** 与**免鉴权的 `/api/health`**（只报数量与键名，不带配置值）；
       运维不查探针也能在健康检查里看到。
     - **`patch_max_total_bytes=0` 与 `patch_max_rollback_bytes=0` 的 0 语义统一**：
       v1.7.27 把前者当"不限制"、后者当"回退内置 8MB 安全上限"，同一个 0 两套解释，
       迟早有人踩。v1.7.28 统一为 **0 / 负数 = 回退内置默认上限**
       （`PATCH_MAX_TOTAL_BYTES` / `DEFAULT_ROLLBACK_CAP`），0 永不表示"不限"。
     - **修掉上版带红测试发版**：v1.7.27 发布时 `tests/v1724_test.py` 是 43/47——
       备份清理改成内联后 `_prune_backup_objects` 变成没人调用的死代码，而那个测试
       正是靠 monkeypatch 它来验证的，于是"孤儿超删"的回归网彻底失效（4 条红但产品
       行为是对的）。v1.7.28 删掉死代码，并把这 4 条红测试改写成**断言行为**
       （孤儿真被删、有效版本真保留；删不掉的孤儿用 OS 级只读模拟而非 monkeypatch
       内部函数，仍如实报出），现 51/51 全绿。
     - `exec_enabled` 默认仍关闭；以上修正完成后才谈是否默认开。
   - **v1.7.29——两条"发版前被低估"的审计项收口**（与 `host:null` 同源的 fail-open）：
     - **`tls_cert` / `tls_key` / `tls_chain` 写成 `null` / 非字符串，或 cert 与 key 只
       配一个，不再静默 fail-open 成明文 HTTP**。`host:null` 修了、`port:null` 拒启了，
       这条同样"配置写了却被悄悄丢掉"的同源问题却一直没修——你以为 HTTPS 在跑、实际
       明文。现在这几种写法都会写进 `config_warnings`（启动横幅 / `win_probe` / `/api/health`
       都带得出，不再空着）；想关 TLS 请显式写空字符串 `""`。新增 `tests/v1729_test.py` 组 A 覆盖。
     - **README 关于 `type .\deploy.ps1` 被拦原因的说明是错的**。旧文档说"首程序 `type`
       不在白名单里就会被拦，加进白名单就能用"——实际拦它的是**围栏⑦ cwd 劫持防护**
       （`.\deploy.ps1` 是 cwd 优先于 PATH 的脚本引用，cwd 里有同名脚本就拒，错误码
       `exec_hijack_suspected`），**即便 `type` 已在白名单也照样拦，加白名单绕不过**。
       按文档排查是白费功夫。已改正文档口径，并说明正确绕法：用绝对路径（`type C:\abs\deploy.ps1`）
       或走文件 API（`win_read` / `win_copy`）。`tests/v1729_test.py` 组 B 用"type 已在白名单"
       为前提证明其仍被 cwd 劫持拦下。
   - 改 Token / 端口后**必须重启服务**；`exec_allowed_commands` / `exec_timeout_sec` /
     `exec_timeout_max_sec` / `exec_max_output_bytes` / `exec_strict_chain` 这几个
     exec 参数是热加载的，保存配置文件后下次调用即生效（`exec_enabled` 能力开关
     仍必须重启）。
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
11. **白名单不是沙箱**（v1.6.4 补充，v1.7.12 更正表述）：
   - 「限制目录」约束的是**本服务的 API**，不是操作系统的隔离边界。AI 往白名单目录里
     写一个 `.bat` / `.ps1` / `.php`，它只是躺在磁盘上的文本 —— **默认配置下没有任何
     "执行"通道**（`win_exec` 整体关闭，tools/list 里不存在），写进去的脚本不会经本服务跑。
   - 但它是"上了膛的枪"：只要服务器上有任何东西会**执行白名单目录里的文件**
     （PHP web 根目录、计划任务的拾取目录、启动文件夹、watcher 服务、有人顺手双击），
     写文件就等于执行代码，权限跟执行者走。`.lnk`、DLL 劫持同类。
     显式开启 `win_exec` 后风险模型再上一级：七重围栏是减误伤的静态检查、
     **不是安全边界**（已知限制逐条列在上文），别把它当沙箱；开启就只给可信
     操作者，并配套 `exec_strict_chain` 与更窄的白名单。
   - 真正的边界是**服务账号 + NTFS 权限**，做法：① 永远不要把"会被自动执行"的目录
     加进白名单（web 根目录宁可只给 `uploads/`、`logs/`）；② 服务用低权限专用账号跑
     （装自启/放防火墙那一下需要管理员，跑服务本身不需要）；③ 敏感目录对该账号显式
     deny，就算脚本逃出来也摸不到。
   - 这不是本工具独有的弱点 —— SFTP、FTP、宝塔等一切"远程写文件"的工具都是同一模型。
     本工具比它们多的是：默认无执行通道、写前自动备份、删除进回收站、全程审计。

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
python tests\features_e2e.py            # 功能回归（标注/轮转/护栏/预校验/工具禁用/启动防护/目录隔离/Token 有效期/HTTPS/只读 Token/备份回滚/tail/tree/批量读/**自签证书**/**证书链与三件套合并**/**数据库与缓存**/**超时边界**）：195~211 用例（无 openssl 时 195）
python tests\mcp_e2e.py                 # MCP 端点（源码）：59 用例
python tests\local_e2e.py               # 本机端到端：13 用例
python tests\sqlsrv_proto_test.py       # SQL Server 驱动协议层（无需真实服务器）：152 用例
python tests\tls_e2e.py                 # 真起 HTTPS 服务端到端（需 openssl，无则跳过）：11 用例
python tests\mcp_exe_test.py            # MCP 端点（打包后的 exe）：59 用例
python tests\mcp_contract_test.py       # initialize 运行时契约 + 逐页还原行为断言：65 用例
python tests\exec_output_test.py        # win_exec 双端压缩预览 + win_tail 白名单口：25 用例
python tests\backup_retention_test.py   # 备份保留策略与配置键集合：26 用例
python tests\write_backup_timeout_test.py  # backup=false 一致性 + 超时上限可配：51 用例
python tests\optimistic_lock_test.py       # win_read(hash=true) + conflict 回传 + win_replace 乐观锁：46 用例
python tests\v1721_test.py                 # 配置分层 + 纯逻辑外提 + 审计降级 + 增量备份 + validate/patch/probe：88 用例
python tests\v1722_test.py              # 响应可序列化 + 围栏⑦转发前缀 + 回滚体积闸：105 用例
python tests\v1723_test.py              # 围栏动态程序名/误伤 + 强制备份 + 硬链接体积 + 只读脱敏 + 文档一致性
python tests\v1724_test.py              # 备份孤儿超删 + exec 已知限制口径 + 契约体积护栏：47 用例
python tests\gui_refresh_test.py        # GUI 异步刷新（tkinter 桩）：52 用例
python test\v1712\spotcheck.py          # win_read 边界 A~F 段：61 用例
python test\v1712\check.py              # 围栏/原子写定点检查：13 用例
```


````

当前状态（v1.7.26 源码级回归）：**selftest 53/53、features 211/211（本机有 openssl）或 195/195（无 openssl，两次都另 2 项真库链路自动跳过）、mcp_e2e 59/59、local 13/13、tls 11/11、sqlsrv_proto 152/152 全通过**；`features_e2e.py` 的用例数随本机是否有 `openssl` 浮动（证书链实测段会整段跳过），不是一个固定值，别拿某个环境跑出的数字当唯一口径。另跑通 `test/v1712/spotcheck.py` A~F 段（61 项）、`check.py` 13 项、`tests/gui_refresh_test.py` 52 项、`tests/mcp_contract_test.py` 65 项、`tests/exec_output_test.py` 25 项、`tests/backup_retention_test.py` 26 项、`tests/write_backup_timeout_test.py` 51 项、`tests/optimistic_lock_test.py` 46 项、`tests/v1721_test.py` 88 项、`tests/v1722_test.py` 105 项、`tests/v1723_test.py` 59 项、`tests/v1724_test.py` 47 项、`tests/v1725_test.py` 54 项、**`tests/v1726_test.py`（本轮新增）**；`py_compile` 与模块导入自检通过。全部 20 套回归 exit=0。

真起临时服务的复核：`test/v1721/patch_wire.py` 实测 `/api/patch` 与 `/mcp` 的 `win_patch` **都返回 200**（修复前是 500 / 连接断），`test/v1721/hijack_variants.py` 的 7 个围栏变体 **7/7 全部拦截**；v1.7.25 复跑两项仍全绿。只查 `isError` 已经放过三次契约错了 —— 现在每个新增字段都要过 `json.dumps` 断言。

> ⚠️ **exe 黑盒仍未在开发机执行**：开发机启用了「智能应用控制」（SAC = On），
> 新构建的自编译 exe 被策略拦截（WinError 4551），因此 `mcp_exe_test.py` 与
> `wkfile.exe --check-gui` 跑不起来。v1.7.17 用同一构建脚本产出的 exe 黑盒
> 56/56 与真窗口自检均通过；v1.7.18 ~ v1.7.25 均未改动 GUI 代码。上述源码级
> 回归已覆盖本轮全部改动（备份孤儿超删与不可回收体积护栏、`would_remove`
> 口径一致、exec 已知限制文案与运行时契约体积护栏）。
> 部署到目标机器后请跑一次 `connect-check.py` 与 `wkfile.exe --check-gui` 自检。

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

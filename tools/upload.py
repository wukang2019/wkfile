#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地文件直传到 wkfile 服务器的 /api/upload 端点（需服务端 v1.7.9+）。

为什么要有它：win_write / win_write_b64 的内容必须塞进 JSON 参数，
大文件（几百 KB 到几 MB）会被参数长度或 base64 膨胀卡住；
/api/upload 走**原始字节流** + sha256 校验，几 MB 也稳。

用法（在**本地开发机**上跑，不是在服务器上）：

    python upload.py --url http://192.168.1.10:8788 --token <主Token> \
                     --from d:\\deving\\kh\\app\\xxx.php \
                     --to   E:\\kanghui\\app\\xxx.php

等价的 curl 写法（Win10+ 自带 curl.exe）：

    curl -X POST --data-binary @xxx.php \
         -H "Authorization: Bearer <主Token>" \
         "http://192.168.1.10:8788/api/upload?path=E%3A%5Ckanghui%5Capp%5Cxxx.php"

说明：
  * 目标路径必须在服务端授权根目录（roots）内，否则 403 out_of_scope
  * 覆盖已有文件前服务端会自动备份（加 --no-backup 可按次关闭）
  * 上传成功会回显服务端算好的 sha256，可再与本地比对
"""

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser(description="本地文件 → wkfile 服务器直传")
    ap.add_argument("--url", required=True,
                    help="服务器根地址，如 http://192.168.1.10:8788")
    ap.add_argument("--token", required=True, help="主 Token（只读 Token 会被拒）")
    ap.add_argument("--from", dest="src", required=True, help="本地文件路径")
    ap.add_argument("--to", dest="dst", required=True,
                    help="服务器上的目标路径（必须在授权根目录内）")
    ap.add_argument("--no-backup", action="store_true",
                    help="覆盖时不做写前备份（默认会备份）")
    args = ap.parse_args()

    try:
        with open(args.src, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        print(f"[X] 读本地文件失败：{exc}")
        return 1

    query = {"path": args.dst, "sha256": hashlib.sha256(data).hexdigest()}
    if args.no_backup:
        query["backup"] = "0"
    url = args.url.rstrip("/") + "/api/upload?" + urllib.parse.urlencode(query)

    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": f"Bearer {args.token}",
                 "Content-Type": "application/octet-stream"},
    )
    # 不走系统代理：内网直连场景里代理常常帮倒忙
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=300) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"[X] 服务端拒绝（HTTP {e.code}）：{body}")
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"[X] 连不上服务器：{exc}")
        return 1

    try:
        resp = json.loads(body)
    except ValueError:
        print(f"[X] 响应不是 JSON：{body[:300]}")
        return 1
    if not resp.get("ok"):
        print(f"[X] 上传失败：{json.dumps(resp.get('error'), ensure_ascii=False)}")
        return 1

    d = resp.get("data") or {}
    print(f"[OK] 已上传 {d.get('bytes')} 字节 → {d.get('path')}")
    print(f"     服务端 sha256：{d.get('sha256')}")
    if d.get("backup"):
        print(f"     覆盖备份：{d.get('backup')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

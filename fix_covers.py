#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修复 apply 阶段 413 失败的封面上传: 压缩超限封面后补传
背景: Komga 对缩略图上传有大小限制, bangumi 部分大图(>1MB)超限; 元数据已写成功, 仅封面缺失
用法:
  python3 fix_covers.py --run-id run_20260929_001251              # dry-run: 只统计与压缩预览
  python3 fix_covers.py --run-id run_20260929_001251 --apply      # 真传
可选: --limit 1048576  (压缩目标字节上限, 默认 1MB)
"""
import argparse
import glob
import io
import os
import re
import sys

import scraper
from scraper import Komga, log

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def shrink(img_bytes, limit):
    """Pillow 循环降参压缩到 limit 以下, 返回新 bytes; Pillow 不可用或压不下去返回 None"""
    try:
        from PIL import Image
    except ImportError:
        return None
    quality = 87
    for maxdim in (1600, 1400, 1200, 1000):
        for _ in range(3):
            im = Image.open(io.BytesIO(img_bytes))
            im = im.convert("RGB")
            w, h = im.size
            if max(w, h) > maxdim:
                r = maxdim / max(w, h)
                im = im.resize((int(w * r), int(h * r)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality, optimize=True)
            b = buf.getvalue()
            if len(b) <= limit:
                return b
            quality -= 10
        quality = max(quality, 55)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=1024 * 1024)
    args = ap.parse_args()

    # 1. 从 apply 日志提取 413 失败的系列 ID
    logs = sorted(glob.glob(os.path.join(BASE_DIR, "logs", "apply_*.log")))
    if not logs:
        sys.exit("无 apply 日志")
    txt = open(logs[-1], encoding="utf-8").read()
    failed_sids = re.findall(r"失败 \[.*?\]: 413 Client Error.*?/series/(\w+)/t", txt)
    failed_sids = sorted(set(failed_sids))
    log(f"413 失败系列: {len(failed_sids)} 个 (apply 日志 {os.path.basename(logs[-1])})")
    if not failed_sids:
        log("无失败, 无需修复")
        return

    # 2. 定位每个系列的封面缓存
    dec = scraper.cache_json(os.path.join(BASE_DIR, "reports", args.run_id, "decisions.json")) or {}
    komga = Komga(scraper.load_config())
    ok = skip = miss = 0
    for sid in failed_sids:
        d = dec.get(sid) or {}
        bgm = d.get("subject_id")
        if not bgm:
            miss += 1
            continue
        fp = os.path.join(BASE_DIR, "cache", "bgm_covers", f"bgm_{bgm}.jpg")
        if not os.path.exists(fp):
            log(f"  缺封面缓存 {sid} (bgm {bgm})")
            miss += 1
            continue
        raw = open(fp, "rb").read()
        data = raw if len(raw) <= args.limit else shrink(raw, args.limit)
        if data is None:
            log(f"  压不下去 {sid} 原图 {len(raw)//1024}KB")
            skip += 1
            continue
        if args.apply:
            komga.upload_cover(sid, data)
            for t in komga.list_thumbs(sid):
                if t.get("type") == "USER_UPLOADED" and not t.get("selected"):
                    komga.delete_thumb(sid, t["id"])
        log(f"  {'写入' if args.apply else '[dry]'} {sid} bgm:{bgm} {len(raw)//1024}KB->{len(data)//1024}KB")
        ok += 1
    log(f"完成: 补传 {ok}, 压缩失败 {skip}, 无缓存 {miss}" + ("" if args.apply else " (dry-run)"))


if __name__ == "__main__":
    main()

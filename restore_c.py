#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C档文件名恢复: bangumi 无数据的系列, 标题回写清洗后的文件夹名, 删除错误 bgm 链接
背景: 老刮削器曾给这些系列写过错误标题/简介/链接 (如 黄金之肩→"球霸"), "保持原样"≠保持文件名
用法:
  python3 restore_c.py --run-id run_20260929_000203            # dry-run (默认, 只打印)
  python3 restore_c.py --run-id run_20260929_000203 --apply    # 真写 (journal 全程记录)
  追加 --clear-extras 同时清空老刮削器写的简介/标签 (错作品的数据)
红线: 尊重锁定字段; 只碰 C 档系列; 写前 journal; 可用 restore.py 按系列回滚
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

import scraper
from scraper import Komga, log, save_json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True, help="决策报告 run id (如 run_20260929_000203)")
    ap.add_argument("--apply", action="store_true", help="真写 (默认 dry-run)")
    ap.add_argument("--clear-extras", action="store_true", help="同时清空简介/标签/别名/出版社")
    ap.add_argument("--clear-covers", action="store_true", help="删除老刮削器上传的系列封面 (回落首卷自动封面)")
    ap.add_argument("--sids-file", help="按清单处理 (每行一个系列ID), 不限 C 档; 此模式不锁字段, 便于审阅后正常刮削写入")
    args = ap.parse_args()

    cfg = scraper.load_config()
    dec = json.load(open(os.path.join(BASE_DIR, "reports", args.run_id, "decisions.json"), encoding="utf-8"))
    titles = json.load(open(os.path.join(BASE_DIR, "cache", "titles.json"), encoding="utf-8"))
    idx = json.load(open(os.path.join(BASE_DIR, "cache", "series_index.json"), encoding="utf-8"))
    c_list = [(sid, d) for sid, d in dec.items() if d["tier"] == "C"]
    lock = True
    if args.sids_file:
        want = [x.strip() for x in open(args.sids_file) if x.strip()]
        c_list = [(sid, dec.get(sid) or {}) for sid in want]
        lock = False
    log(f"文件名恢复: {len(c_list)} 个系列, clear_extras={args.clear_extras}, clear_covers={args.clear_covers}, "
        f"lock={lock}, apply={args.apply}")

    komga = Komga(cfg)
    run_dir = os.path.join(BASE_DIR, "journal", f"crestore_{dt.datetime.now():%Y%m%d_%H%M%S}")
    if args.apply:
        os.makedirs(run_dir, exist_ok=True)

    changed = skipped = 0
    for sid, d in sorted(c_list, key=lambda x: idx[x[0]]["name"]):
        clean = (titles.get(sid) or {}).get("title") or idx[sid]["name"]
        fresh = komga.series_detail(sid)
        cur = fresh.get("metadata") or {}
        if cur.get("titleLock"):
            log(f"  跳过(锁定) {idx[sid]['name'][:30]}")
            skipped += 1
            continue
        keep_links = [l for l in (cur.get("links") or [])
                      if not re.search(r"(bgm\.tv|bangumi\.tv|chii\.in)/subject/", l.get("url") or "")]
        payload = {"title": clean, "titleSort": clean, "links": keep_links,
                   "titleLock": lock, "titleSortLock": lock, "linksLock": lock}
        if args.clear_extras:
            payload.update({"summary": "", "tags": [], "alternateTitles": [], "publisher": "",
                            "summaryLock": lock, "tagsLock": lock, "alternateTitlesLock": lock,
                            "publisherLock": lock})
        if args.apply:
            save_json(os.path.join(run_dir, f"{sid}.json"),
                      {"series_id": sid, "name": fresh.get("name"), "metadata": cur})
        old = cur.get("title") or "(无)"
        if args.apply:
            komga.patch_metadata(sid, payload)
            if args.clear_covers:
                thumbs = komga.list_thumbs(sid)
                save_json(os.path.join(run_dir, f"{sid}.thumbs.json"), thumbs)
                for t in thumbs:
                    if t.get("type") == "USER_UPLOADED":
                        komga.delete_thumb(sid, t["id"])
            log(f"  写入 {idx[sid]['name'][:28]}: {old[:18]} -> {clean[:22]}")
        else:
            log(f"  [dry] {idx[sid]['name'][:28]}: {old[:18]} -> {clean[:22]}")
        changed += 1
    log(f"完成: 处理 {changed}, 跳过(锁定) {skipped}"
        + (f", journal -> {run_dir}" if args.apply else " (dry-run 未写入)"))


if __name__ == "__main__":
    main()

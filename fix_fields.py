#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全字段统一处理: 清除老刮削器两层污染的最后残留
系列级: status←文件夹完/未完真理 | genres/ageRating 清空 | A档+releaseDate(bgm发售日)
分册级: 标题→文件名 | 简介清空 | 删老错封面(回落分册首页)
用法:
  python3 fix_fields.py --run-id run_20260929_001251           # dry-run
  python3 fix_fields.py --run-id run_20260929_001251 --apply   # 真写 (journal 全程)
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

import scraper
from scraper import Komga, RateLimiter, log

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def folder_status(name):
    """文件夹名 → Komga status; 无法判定返回 None (不动)"""
    if re.search(r"未完|\d+\s*未\]|连载中|連載中", name):
        return "ONGOING"
    if re.search(r"\d+\s*完|全\s*\d+\s*[集卷]|完结|完\]", name):
        return "ENDED"  # Komga 枚举是 ENDED, 不是 COMPLETED (实测 400)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rps", type=float, default=8, help="写入限速 (本机回环, 30 实测无压力)")
    ap.add_argument("--snapshot", help="复用已有快照路径 (中断续跑时用, 须早于本批所有写入)")
    args = ap.parse_args()
    cfg = scraper.load_config()
    komga = Komga(cfg)
    dec = scraper.cache_json(os.path.join(BASE_DIR, "reports", args.run_id, "decisions.json")) or {}
    lib = komga.find_library(cfg["target"]["library_name"])
    rl = RateLimiter(args.rps)
    journal = {"series": {}, "books": {}}
    jfp = os.path.join(BASE_DIR, "journal", f"fix_fields_{dt.datetime.now():%Y%m%d_%H%M%S}.json")
    if args.apply:
        os.makedirs(os.path.dirname(jfp), exist_ok=True)
        # 红线: 无新鲜快照不写 (分册封面删除只能靠 DB 快照回滚)
        if args.snapshot:
            if not os.path.isfile(args.snapshot):
                sys.exit(f"快照不存在: {args.snapshot}, 拒绝写入")
            log(f"复用快照: {args.snapshot}")
        else:
            r = subprocess.run(["bash", os.path.join(BASE_DIR, "snapshot_db.sh")])
            if r.returncode != 0:
                sys.exit("快照失败, 拒绝写入")

    stat = {"series_status": 0, "genres_clear": 0, "age_clear": 0, "release": 0,
            "book_title": 0, "book_summary": 0, "book_thumb": 0, "skip": 0, "lock_skip": 0, "error": 0}
    journal["thumbs_deleted"] = []
    journal["errors"] = []

    def save():
        if args.apply:
            json.dump(journal, open(jfp, "w"), ensure_ascii=False, indent=1)

    def do(desc, fn):
        """单项写失败只记错不中断; 每次写后落盘 journal (防崩溃丢记录)"""
        try:
            rl.wait()
            r = fn()
            if r is not None and hasattr(r, "raise_for_status"):
                r.raise_for_status()
            return True
        except Exception as e:
            stat["error"] += 1
            journal["errors"].append(f"{desc}: {e}")
            log(f"  ! {desc}: {e}")
            return False

    try:
        run(komga, lib, dec, args, rl, journal, stat, save, do)
    finally:
        save()
    log(("=== APPLY" if args.apply else "=== DRY-RUN") + f" 完成: {json.dumps(stat, ensure_ascii=False)}")
    if args.apply:
        log(f"journal -> {jfp}")


def run(komga, lib, dec, args, rl, journal, stat, save, do):
    for i, s in enumerate(komga.all_series(lib["id"])):
        sid = s["id"]
        tier = (dec.get(sid) or {}).get("tier")
        m = s.get("metadata") or {}
        payload = {}
        st = folder_status(s["name"])
        if st and m.get("status") != st and not m.get("statusLock"):
            payload["status"] = st
        if m.get("genres") and not m.get("genresLock"):
            payload["genres"] = []
        if m.get("ageRating") and not m.get("ageRatingLock"):
            payload["ageRating"] = None
        for f in ("status", "genres", "ageRating"):
            if m.get(f + "Lock"):
                stat["lock_skip"] += 1
        # 注: Komga 系列元数据没有 releaseDate 字段 (PATCH 静默忽略), 发售日只能来自分册, 已删除该逻辑
        if payload:
            if args.apply:
                journal["series"][sid] = {"old": {k: m.get(k) for k in payload}, "new": payload}
                if not do(f"series {sid}", lambda: komga.patch_metadata(sid, payload)):
                    journal["series"].pop(sid, None)
                save()
            for k in payload:
                stat[{"status": "series_status", "genres": "genres_clear",
                      "ageRating": "age_clear"}[k]] += 1
        # 分册级
        page = 0
        while True:
            d = komga.get_json(f"/api/v1/series/{sid}/books?page={page}&size=100")
            for b in d.get("content") or []:
                bm = b.get("metadata") or {}
                bp = {}
                if bm.get("title") and bm["title"] != b.get("name", "").strip():  # Komga 会 trim 标题, 文件名带前导空格时否则每次重写
                    if bm.get("titleLock"):
                        stat["lock_skip"] += 1
                    else:
                        bp["title"] = b["name"].strip()
                if bm.get("summary"):
                    if bm.get("summaryLock"):
                        stat["lock_skip"] += 1
                    else:
                        bp["summary"] = ""
                if bp:
                    if args.apply:
                        bid = b["id"]
                        journal["books"][bid] = {"old": {k: bm.get(k) for k in bp}, "new": bp}
                        if not do(f"book {bid}", lambda: komga.s.patch(
                                f"{komga.base}/api/v1/books/{bid}/metadata", json=bp, timeout=komga.timeout)):
                            journal["books"].pop(bid, None)
                    for k in bp:
                        stat["book_title" if k == "title" else "book_summary"] += 1
                thumbs = komga.get_json(f"/api/v1/books/{b['id']}/thumbnails")
                for t in thumbs:
                    if t.get("type") == "USER_UPLOADED":
                        if args.apply:
                            url = f"{komga.base}/api/v1/books/{b['id']}/thumbnails/{t['id']}"
                            if do(f"thumb {b['id']}/{t['id']}", lambda: komga.s.delete(url, timeout=komga.timeout)):
                                journal["thumbs_deleted"].append([b["id"], t["id"]])
                        stat["book_thumb"] += 1
            if d.get("last", True):
                break
            page += 1
        save()
        if (i + 1) % 100 == 0:
            log(f"  进度 {i+1} 系列 | {json.dumps(stat, ensure_ascii=False)}")


if __name__ == "__main__":
    main()

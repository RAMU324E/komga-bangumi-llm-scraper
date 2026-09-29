#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核查全库 bgm 条目类型 (只读): ①写成了小说条目 ②写成了单卷但其实有系列条目
输出 reports/A档条目类型核查.md, 不改 Komga 任何数据"""
import os, re
import scraper
from scraper import log, volume_to_series

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
cfg = scraper.load_config()
k = scraper.Komga(cfg)
bg = scraper.Bangumi(cfg)
paths = {"cache": "cache", "pages": "cache/pages", "bgm_subjects": "cache/bgm_subjects",
         "bgm_covers": "cache/bgm_covers", "journal": "journal"}
lib = k.find_library(cfg["target"]["library_name"])
pub = ((cfg.get("report", {}) or {}).get("public_base_url") or "").rstrip("/")
rows_nov, rows_vol, n_link = [], [], 0
for s in k.all_series(lib["id"]):
    m = s.get("metadata") or k.series_detail(s["id"])["metadata"]
    hit = re.search(r"(?:bgm|bangumi)\.tv/subject/(\d+)", " ".join(l.get("url") or "" for l in (m.get("links") or [])))
    if not hit:
        continue
    n_link += 1
    sub = bg.subject(int(hit.group(1)), paths["bgm_subjects"])
    if not sub:
        continue
    plat = sub.get("platform") or "?"
    link = f"[系列 {s['id']}]({pub}/series/{s['id']})" if pub else s["id"]
    base = (f"| {s['name'][:46]} | {sub['id']} [{plat}] {(sub.get('name_cn') or sub.get('name'))[:24]} "
            f"| {link} | ")
    if plat == "小说":
        rows_nov.append(base + "换漫画版 |")
    elif plat == "漫画" and not sub.get("series"):
        ser = [r for r in (bg.related(sub["id"], paths) or []) if r.get("relation") == "系列" and r.get("type") == 1]
        if ser:
            nm = (ser[0].get("name_cn") or ser[0].get("name") or "")[:20]
            rows_vol.append(base + f"换系列 {ser[0]['id']} {nm} |")
out = ["# A 档条目类型核查（只读，未修改数据）", "",
       f"共扫描 {n_link} 个带 bgm 链接的系列。修复方案已进刮削器（单卷→系列、小说→漫画），确认后按清单批量改。", "",
       f"## ① 写成了小说条目（{len(rows_nov)}）", "",
       "| 文件夹 | 当前条目 | 链接 | 建议 |", "|---|---|---|---|"] + rows_nov + ["",
       f"## ② 写成了单卷、但有系列条目（{len(rows_vol)}）", "",
       "| 文件夹 | 当前条目 | 链接 | 建议 |", "|---|---|---|---|"] + rows_vol + [""]
open("reports/A档条目类型核查.md", "w", encoding="utf-8").write("\n".join(out))
log(f"核查完成: 小说 {len(rows_nov)}, 可换单卷→系列 {len(rows_vol)} -> reports/A档条目类型核查.md")

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三方核对审计: 记忆alt vs 网证原名 vs 被选bgm条目 + 结构红旗扫描
用法: python3 audit.py                       # 只做 alt 对账
      python3 audit.py reports/run_X/decisions.json   # 追加 A 档三方审计"""
import json, re, difflib, os, sys

def norm(s):
    if not s: return ""
    s = s.lower()
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in s)
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[·・\-—_:：!！?？~～()（）\[\]【】.,。、;；]", "", s)
    return s

def sim(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()

def close(a, b):
    if not a or not b: return False
    return a == b or sim(a, b) >= 0.85 or a in b or b in a

def reconcile():
    web = json.load(open("cache/web_alts.json"))
    titles = json.load(open("cache/titles.json"))
    agree = disagree = no_alt = no_web = 0
    diffs = []
    for sid, t in titles.items():
        w = (web.get(sid) or {}).get("japanese_title")
        c = (web.get(sid) or {}).get("confidence") or 0
        a = t.get("alt")
        if not a:
            no_alt += 1
            continue
        if not w or c < 70:
            no_web += 1
            continue
        if close(norm(a), norm(w)):
            agree += 1
        else:
            disagree += 1
            diffs.append((t.get("title", "")[:22], a[:26], w[:26], c))
    tot = agree + disagree
    print("=== 记忆alt vs 网证原名 对账 ===")
    print(f"可比对 {tot} | 一致 {agree} ({agree * 100 // max(tot, 1)}%) | 不一致 {disagree} | 无alt {no_alt} | 网证null/低置信 {no_web}")
    for d in diffs[:20]:
        print(f"  不一致: {d[0]} | 记忆:{d[1]} | 网证:{d[2]} (conf{d[3]})")
    return diffs

def audit_a(dec_path):
    dec = json.load(open(dec_path))
    titles = json.load(open("cache/titles.json"))
    idx = json.load(open("cache/series_index.json"))
    web = json.load(open("cache/web_alts.json"))
    a_rows = [(sid, d) for sid, d in dec.items() if d["tier"] == "A"]
    clean, flagged = [], []
    seen = {}
    vol_pat = re.compile(r"[（(]\s*\d+\s*[)）]\s*$")
    for sid, d in a_rows:
        sfp = f"cache/bgm_subjects/{d['subject_id']}.json"
        if not os.path.exists(sfp):
            continue
        sub = json.load(open(sfp))
        sj = norm(sub.get("name") or "")
        sc = norm(sub.get("name_cn") or "")
        w = (web.get(sid) or {}).get("japanese_title")
        nw = norm(w) if w else ""
        flags = []
        if d["subject_id"] in seen:
            flags.append(f"重复匹配(与{idx[seen[d['subject_id']]]['name'][:12]})")
        seen.setdefault(d["subject_id"], sid)
        if vol_pat.search((sub.get("name") or "")) or vol_pat.search((sub.get("name_cn") or "")):
            flags.append("卷条目")
        if nw and not (close(nw, sj) or close(nw, sc)):
            flags.append(f"网证不符:{(w or '')[:18]}")
        row = (idx[sid]["name"][:26], sub.get("name_cn") or sub.get("name"), d["subject_id"], d.get("confidence", "?"))
        (flagged if flags else clean).append((row, flags))
    print(f"=== A档三方审计: 共 {len(a_rows)} | 三方一致 {len(clean)} | 有红旗 {len(flagged)} ===")
    for row, flags in flagged[:30]:
        print(f"  红旗: {row[0]} -> {row[1]} | {'; '.join(flags)}")

if __name__ == "__main__":
    reconcile()
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        audit_a(sys.argv[1])
# Komga × Bangumi 自动刮削器（komga-bangumi-llm-scraper）

> 以「文件夹原名 + 第一话原始封面」为真理源：Bangumi 检索 + 多模态大模型
> （**标题 + 封面 + 简介**三方交叉）裁决，高置信才写入，**全程可逆**。
> 前身是很久之前朋友写的油猴脚本 [dyphire/KomgaBangumi](https://github.com/dyphire/KomgaBangumi)。

## 核心特性

- **判档流水线**：文件夹名清洗 → Bangumi v0 检索（中/日文名、联网补全原名）→ 规则打分
  → 多模态 VLM 裁决 → A 档直写 / B 档人工审 / C 档保持文件名
- **漫画专用纠偏**（自动，无需人工）：
  - 候选剔除**小说条目**；候选全是小说时沿 Bangumi 条目关联自动补**漫画版**
  - 选中**单卷条目**时自动换**系列条目**（沿「系列」关联，找不到则去卷号重搜）
- **AI 拒绝重试**：置信不足的再问 3 次，≥2 次认定同一条目才采纳，排除偶发波动
- **多版本友好**：同一条目被多个文件夹引用时显示标题带（xx版）后缀，互不覆盖；
  `dup_editions.py` 可对存量库体检并自动补后缀
- **搜索增强**：文件夹名写入系列别名（label=文件名），Komga 搜索可直接命中文件名
- **增量安全**：journal 判定已刮系列自动跳过；`skip_series.json` 永久跳过名单；
  尊重用户手动锁定的字段
- **三层备份**：VACUUM INTO 全库快照 / 逐系列 journal（含 LOCK 标志）/ 元数据表转储，
  单系列可 API 级零停机回滚
- **全链路缓存**：titles/searches/web_alts/bgm_subjects/bgm_relations/scores/vlm
  全部落盘，断点续跑秒级出决策

## 快速开始

```bash
# 依赖: Python 3.9+
pip install -r requirements.txt

cp config.example.json config.json   # 填入 Komga/DeepSeek key, chmod 600
python3 scraper.py                   # 1. dry-run (只读, 出报告)
# 2. 人工审 reports/run_*/report.md 的 B 档区
python3 scraper.py --apply --covers  # 3. 真写 (强制先快照)
```

常用参数：`--series ID1,ID2` 指定系列；`--exclude ID1,ID2` 强制降 B 档；`--force` 重刮。

新系列日常增量：Komga 扫描出新系列 → `python3 scraper.py`（有缓存，只对新系列调 API）
→ 日志末尾「增量预览」列出将写的系列 → 确认后 `--apply --covers`。

## 配置（config.json）

| 键 | 说明 |
|---|---|
| `komga.base_url` / `api_key` | Komga 地址与管理员 API key（只写这一个库） |
| `komga.database_path` | 可选；Komga 的 database.sqlite 路径，用于 apply 后的元数据表转储（备份层③） |
| `deepseek.*` | 多模态模型（标题清洗、VLM 裁决、联网原名补全） |
| `bangumi.*` | base_url / user_agent（建议带联系方式）/ 限速 |
| `target.library_name` | **库白名单**：只处理这个 library，其他库物理隔离 |
| `match.*` | 阈值：exact_score=95 / vlm_accept=85 / vlm_review_floor=60 / vlm_retry=3 等 |
| `report.public_base_url` | 报告里系列链接的站点前缀（可留空） |
| `apply.*` | write_covers / lock_written_fields / respect_locked_fields / writes_per_second |

## 工具箱

| 脚本 | 用途 |
|---|---|
| `scraper.py` | 主程序（采集/清洗/搜索/打分/VLM/归一化/报告/写入） |
| `restore.py` | journal 还原：`--run-id X [--series id1,id2] [--covers]`，API 级零停机 |
| `restore_c.py` | C 档文件名恢复：清掉旧刮削器/错误来源的元数据（`--clear-extras`） |
| `fix_fields.py` | 全字段统一（完结状态/genres/分级/出版日期/分册标题与封面） |
| `fix_covers.py` | 补传超限封面（Pillow 压缩到 ≤1MB） |
| `add_filename_aliases.py` | 全库文件夹名写入别名，让搜索命中文件名 |
| `dup_editions.py` | 同名显示标题体检 + 自动加（xx版）后缀；防撞车（撞车自动升级"基础+卷数"），显示已互异的组不动，选不出后缀宁可跳过；默认 dry-run |
| `audit_subject_type.py` | 核查已刮条目类型：写错成小说/可换单卷→系列（只读报告） |
| `snapshot_db.sh` | 全库快照（环境变量 `KOMGA_DATABASE`/`KOMGA_SNAPSHOT_DIR`） |

## 红线（写死在代码里）

1. 库白名单：只处理 `target.library_name` 指定的 library
2. 只写 `komga.base_url` 指定的那一个 Komga，不碰其他实例
3. 无快照不写入、无 journal 不写入、置信度不足不写入
4. Bangumi 限速 + 规范 UA；模型调用失败重试后降级处理，不当作否决
5. 尊重用户手动锁定的字段（`respect_locked_fields` 默认 true）

## 实战经验

- **Komga 缩略图上传限制 ≈1MB**：大图会 413，内置 Pillow 压缩（`compress_jpeg`）
- **DeepSeek 联网搜索**：Responses API 的 `web_search` 参数被官方忽略；真实入口是
  Anthropic 兼容端点 `/anthropic/v1/messages` + `web_search` 工具（phase_webalt 已集成）
- **Bangumi 限速**：5 rps 实测无压力；条目关联接口（`/v0/subjects/{id}/subjects`）
  是单卷→系列、小说→漫画的数据基础，带缓存
- **幂等**：写入按 journal 判定增量；SSH 断连导致远端脚本可能已执行完，重跑会自动跳过已处理项
- **别名搜索**：Komga 搜索匹配标题+别名，但不匹配文件夹名——所以要靠别名补

## 目录布局

```
komga-bangumi-llm-scraper/
  scraper.py  restore.py  restore_c.py  fix_fields.py  fix_covers.py
  add_filename_aliases.py  dup_editions.py  audit_subject_type.py
  snapshot_db.sh  requirements.txt  LICENSE
  config.example.json      # 复制为 config.json 填入真实 key (chmod 600, 勿提交)
  .gitignore
  cache/     # 全链路缓存 (titles/searches/bgm_subjects/bgm_relations/scores/vlm/...)
  journal/   # 每次写入的逐系列完整快照 (回滚依据)
  reports/   # 每次 run 的 decisions.json + report.md
  logs/
```

## 判定流水线

```
系列 → ① 原名采集: 文件夹名 + 第一话第1页原图(从文件直读, 不受旧元数据污染)
     → ② 标题清洗: LLM 解析汉化组前缀/卷号/副标题 → 干净标题+变体
     → ③ Bangumi 搜索: v0 API (type=1 书籍, 剔除小说), 中/日文名都搜, 全程缓存
     → ④ 规则打分: 标题精确/模糊 + 年份/作者加分
          ├─ 精确命中(≥95) ───────────────→ A 档
          ├─ 40~94 ──→ ⑤ VLM 裁决 ──→ ≥85 A / 60~84 B(人工审) / <60 C
          └─ <40 或无候选 ─────────────────→ C 档(保持文件名)
     → ⑥ 归一化: 小说→漫画版, 单卷→系列
     → ⑦ dry-run 报告 → 人工审 → --apply 真写 (快照 + journal + 核对)
```

VLM 每个系列输入：原始文件夹名、清洗标题、第一话第1页原图；每个候选：原名/中文名/
发售日/标签/封面大图/简介。要求综合「标题文字 + 封面视觉 + 简介语义」三重一致，

## 许可

[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.zh-hans)：
非商业使用；衍生作品须以相同协议开源。详见 [LICENSE](LICENSE)。
输出 JSON `{subject_id, confidence, reason}`；解析失败一律按 C 档（宁可漏，不可错）。

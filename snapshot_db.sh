#!/usr/bin/env bash
# ============================================================================
# 刮削前主库快照: VACUUM INTO 在线一致性快照 (Komga 不停机, 读者无感)
# scraper.py --apply 会强制先执行本脚本, 无快照拒绝写入
#
# 路径通过环境变量覆盖 (也可写进 systemd/cron 环境):
#   KOMGA_DATABASE      Komga 的 database.sqlite 路径 (必填)
#   KOMGA_SNAPSHOT_DIR  快照输出目录 (默认 ./snapshots)
#   KOMGA_SNAPSHOT_KEEP 保留份数 (默认 5)
# ============================================================================
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
set -euo pipefail

SRC="${KOMGA_DATABASE:?请设置 KOMGA_DATABASE 指向 Komga 的 database.sqlite}"
DEST_DIR="${KOMGA_SNAPSHOT_DIR:-./snapshots}"
KEEP="${KOMGA_SNAPSHOT_KEEP:-5}"

mkdir -p "$DEST_DIR"
ts=$(date '+%Y%m%d_%H%M%S')
out="$DEST_DIR/db_snapshot_$ts.sqlite"

sqlite3 -cmd ".timeout 15000" "$SRC" "VACUUM INTO '$out';"

check=$(sqlite3 "$out" "PRAGMA quick_check;")
if [ "$check" != "ok" ]; then
  echo "ERROR: 快照完整性校验失败: $check" >&2
  rm -f "$out"
  exit 1
fi

echo "快照完成: $out ($(du -h "$out" | cut -f1))"
ls -1t "$DEST_DIR"/db_snapshot_*.sqlite | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "保留最近 $KEEP 份"

cat <<'EOF'

如需全库回退 (Komga 短暂停机):
  停止 Komga (如 docker stop <komga容器>)
  cd <Komga config 目录>
  rm -f database.sqlite-wal database.sqlite-shm
  cp -a <快照目录>/db_snapshot_XXXX.sqlite database.sqlite
  启动 Komga
  注意: 快照到回退期间产生的阅读进度会丢失
EOF

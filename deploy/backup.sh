#!/bin/sh
# Sao lưu Postgres (chạy bằng cron mỗi ngày): 0 3 * * * /opt/coin/deploy/backup.sh
# Giữ 14 bản gần nhất. Bản sao lưu chứa lịch sử lệnh/audit nên cất ở nơi chỉ root đọc được.
set -eu
cd "$(dirname "$0")/.."
DEST="${BACKUP_DIR:-/var/backups/coin}"
mkdir -p "$DEST"
umask 077
docker compose exec -T coin-db pg_dump -U coin coin | gzip > "$DEST/coin-$(date +%Y%m%d-%H%M).sql.gz"
ls -1t "$DEST"/coin-*.sql.gz | tail -n +15 | xargs -r rm --

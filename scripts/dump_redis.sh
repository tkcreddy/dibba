#!/bin/bash
OUTFILE="redis_dump.txt"
HOST="redis-server"
PORT=6379
CA="/opt/dibba/config/ssl/ca.crt"
CERT="/opt/dibba/config/ssl/client.crt"
KEY="/opt/dibba/config/ssl/client.key"

> "$OUTFILE"   # empty file first

for db in $(seq 0 15); do
  echo "=== DB $db ===" >> "$OUTFILE"
  redis-cli --tls \
    --cacert "$CA" --cert "$CERT" --key "$KEY" \
    -h "$HOST" -p "$PORT" -n "$db" --scan | while read key; do
      val=$(redis-cli --tls \
        --cacert "$CA" --cert "$CERT" --key "$KEY" \
        -h "$HOST" -p "$PORT" -n "$db" GET "$key")
      echo "$key => $val" >> "$OUTFILE"
  done
done

echo "✅ Export complete: $OUTFILE"


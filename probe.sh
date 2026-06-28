#!/usr/bin/env bash
NS=$(grep nameserver /etc/resolv.conf | head -1 | cut -d' ' -f2)
GW=$(ip route | grep '^default' | head -1 | cut -d' ' -f3)
echo "nameserver=$NS  gateway=$GW"
echo "--- scanning common proxy ports on Windows host ---"
for HOST in 127.0.0.1 "$GW" "$NS"; do
  [ -z "$HOST" ] && continue
  for PORT in 7890 7897 10808 10809 1080 2080 20171 33210; do
    if timeout 1 bash -c "echo > /dev/tcp/$HOST/$PORT" 2>/dev/null; then
      echo "OPEN  $HOST:$PORT"
    fi
  done
done
echo "(done — any OPEN line above is a reachable proxy)"

#!/bin/bash
# Pinggy 免费隧道：SSH 套 TLS 走 443（沙箱只放行 TLS/443）-> localhost:8001
# 免费版每 60 分钟断开，自动重连，但地址会变；最新地址写入 TUNNEL_URL.txt
# 启动: nohup ./tunnel.sh >/dev/null 2>&1 &    停止: pkill -f tunnel.sh; pkill -f a.pinggy.io
cd /workspace/asset-tracker
while true; do
  ssh -T -p 443 -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
    -o "ProxyCommand=openssl s_client -quiet -connect a.pinggy.io:443 -servername a.pinggy.io" \
    -R0:localhost:8001 free@a.pinggy.io 2>&1 | while IFS= read -r l; do
      echo "$(date '+%F %T') $l" >> tunnel.log
      case "$l" in https://*) echo "$l" > TUNNEL_URL.tmp; grep -h https TUNNEL_URL.tmp >> TUNNEL_URL.new;; esac
      [ -f TUNNEL_URL.new ] && [ $(wc -l < TUNNEL_URL.new) -ge 2 ] && mv TUNNEL_URL.new TUNNEL_URL.txt
    done
  rm -f TUNNEL_URL.new TUNNEL_URL.tmp; sleep 3
done

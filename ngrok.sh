#!/bin/bash
# Fixed free ngrok domain -> localhost:8001
cd /workspace/asset-tracker
BIN=/home/box/bin/ngrok
DOMAIN=schilling-enforced-rename.ngrok-free.dev
pkill -f "$BIN http" 2>/dev/null || true
sleep 1
# refresh local CRL cache via https if present
mkdir -p /tmp/ngrok-crl
curl -fsSL -m 15 -o /tmp/ngrok-crl/ngrok.crl https://crl.ngrok-agent.com/ngrok.crl || true
nohup "$BIN" http --url="https://$DOMAIN" 8001 --log=stdout >> ngrok.log 2>&1 &
echo $! > ngrok.pid
echo "https://$DOMAIN" > TUNNEL_URL.txt
sleep 3
curl -s http://127.0.0.1:4040/api/tunnels | head -c 400; echo

#!/bin/bash
# Run on VPS: bash pull.sh
REPO="https://raw.githubusercontent.com/ghostybox01/z/main"
CORE=/opt/synthtel/core
WEB=/var/www/html

for f in server.py safe_urlopen.py urlopen_compat.py ssh_helper.py \
          campaign.py api_sender.py b2b_manager.py crm_sender.py \
          email_checker.py email_sorter.py imap_extractor.py link_encoder.py \
          mime_builder.py mx_sender.py o365_relay.py owa_sender.py smtp_sender.py \
          spam_filter.py suppression_list.py tags.py telegram_bot.py tunnel_manager.py; do
  curl -fsSL "$REPO/core/$f" -o "$CORE/$f" && echo "✓ $f" || echo "✗ $f"
done

curl -fsSL "$REPO/index.html" -o "$WEB/index.html" && echo "✓ index.html" || echo "✗ index.html"

systemctl restart synthtel && echo "✓ restarted"
echo -n "Waiting for server..."
for i in $(seq 1 30); do
  sleep 1
  if curl -sf http://127.0.0.1:5001/api/ping -o /dev/null 2>/dev/null; then
    echo " ready (${i}s)"
    break
  fi
  echo -n "."
  if [ "$i" -eq 30 ]; then echo " timed out — check: journalctl -u synthtel -n 50"; fi
done

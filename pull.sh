#!/bin/bash
# Run on VPS: bash pull.sh
REPO="https://raw.githubusercontent.com/ghostybox01/z/main"
CORE=/opt/synthtel/core
WEB=/var/www/html

for f in server.py campaign.py api_sender.py b2b_manager.py crm_sender.py \
          email_checker.py email_sorter.py imap_extractor.py link_encoder.py \
          mime_builder.py mx_sender.py o365_relay.py owa_sender.py smtp_sender.py \
          spam_filter.py suppression_list.py tags.py telegram_bot.py tunnel_manager.py; do
  curl -fsSL "$REPO/core/$f" -o "$CORE/$f" && echo "✓ $f" || echo "✗ $f"
done

curl -fsSL "$REPO/index.html" -o "$WEB/index.html" && echo "✓ index.html" || echo "✗ index.html"

systemctl restart synthtel && echo "✓ restarted"

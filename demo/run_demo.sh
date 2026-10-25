#!/usr/bin/env bash
# Re-creates the demo in POST.md. Needs: ollama serving gemma3:4b on localhost.
set -u
export PYTHONUNBUFFERED=1
D=$(mktemp -d)/Downloads
python demo/make_messy.py "$D" >/dev/null
cd "$(dirname "$D")"
p() { echo; echo "\$ $*"; "$@"; }
p ls Downloads
p sloth-tidy Downloads
p sloth-tidy Downloads --apply
p find Downloads -type f -not -path '*/.sloth-tidy/*' | sort
p sloth-tidy undo Downloads
p ls Downloads
echo; echo "# what happens when the model isn't running"
p sloth-tidy Downloads --host http://127.0.0.1:9 
echo; echo "# and when someone points it at a remote server"
p sloth-tidy Downloads --host http://8.8.8.8:11434

echo; echo "# watch mode: a file lands, sits still for a bit, then gets tidied"
mkdir -p Inbox
( sleep 2; printf 'Order #A-99812 confirmed. Mechanical keyboard, Rs 3,499. Thanks for shopping with us.' > "Inbox/mail-attachment.txt"; echo "[demo] dropped Inbox/mail-attachment.txt at $(date +%T)" ) &
echo "\$ sloth-tidy Inbox --watch --apply --every 5 --min-age 8   (stopped after 25s)"
timeout -s INT 25 sloth-tidy Inbox --watch --apply --every 5 --min-age 8
wait
p find Inbox -type f -not -path '*/.sloth-tidy/*'

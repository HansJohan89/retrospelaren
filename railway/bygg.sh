#!/bin/sh
# Bygger railway/function.ts: servern + index.html inbäddad i en avslutande kommentar.
cd "$(dirname "$0")/.."
if grep -q '\*/' index.html; then echo "index.html får inte innehålla */"; exit 1; fi
{ cat railway/server.ts; printf '\n/* INDEX_HTML\n'; cat index.html; printf '\nINDEX_HTML */\n'; } > railway/function.ts
echo "railway/function.ts: $(wc -c < railway/function.ts) byte"

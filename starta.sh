#!/bin/sh
cd "$(dirname "$0")"
( sleep 1; (command -v xdg-open >/dev/null && xdg-open "http://127.0.0.1:8888/") || (command -v open >/dev/null && open "http://127.0.0.1:8888/") ) >/dev/null 2>&1 &
exec python3 server.py "$@"

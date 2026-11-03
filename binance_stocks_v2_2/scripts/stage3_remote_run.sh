#!/bin/bash
set -a
. /home/ubuntu/.config/bstocks_v2/stage2.env
set +a
cd /home/ubuntu/binance_stocks_v2_2
export PYTHONPATH=src
exec .venv/bin/python /tmp/stage3_readonly_probe.py

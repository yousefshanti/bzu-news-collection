#!/data/data/com.termux/files/usr/bin/bash
# Runs the remaining collection on the tablet (Termux), detached from any SSH session.
# Start:  cd ~/bzu && nohup setsid bash run_tablet.sh >/dev/null 2>&1 &
# Order: every remaining source first, then reuters + ap, then the quality report.
# Resumable: rerunning skips finished work (state lives in articles.db).
cd "$(dirname "$0")" || exit 1
mkdir -p logs
termux-wake-lock 2>/dev/null          # keep the CPU/network awake with the screen off
export PYTHONUNBUFFERED=1
echo "$$" > logs/tablet_runner.pid
FIRST=$(python3 -c "import json;print(','.join(s['id'] for s in json.load(open('sources.json')) if s['id'] not in ('reuters','ap')))")
echo "$(date '+%F %T') start pull (all but reuters,ap)" >> logs/tablet_runner.log
python3 -W ignore collect.py pull --sources "$FIRST" >> logs/tablet_pull1.out 2>&1
echo "$(date '+%F %T') start pull (reuters,ap) exit=$?" >> logs/tablet_runner.log
python3 -W ignore collect.py pull --sources reuters,ap >> logs/tablet_pull2.out 2>&1
echo "$(date '+%F %T') report exit=$?" >> logs/tablet_runner.log
python3 -W ignore collect.py report >> logs/tablet_report.out 2>&1
echo "$(date '+%F %T') DONE" >> logs/tablet_runner.log
termux-wake-unlock 2>/dev/null

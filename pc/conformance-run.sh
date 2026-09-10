#!/usr/bin/env bash
# Run the AMWA suites Atoll can be tested against, collect pass/fail into /tmp/score.tsv
sudo -n docker rm -f nmos-score >/dev/null 2>&1
sudo -n docker run -d --name nmos-score --entrypoint sleep amwa/nmos-testing:latest 7200 >/dev/null 2>&1
sleep 2
cat > /tmp/scoreparse.py <<'PYEOF'
import json,sys
from collections import Counter
try:
    d=json.load(open(f"/tmp/{sys.argv[1]}.json"))
    res=d if isinstance(d,list) else (d.get("results") or d.get("tests") or [])
    c=Counter((t.get("state") or "?") for t in res)
    print(f"{sys.argv[1]}\t{c.get('Pass',0)}\t{c.get('Fail',0)}\t{sum(c.values())}\t"+";".join(f"{k}:{v}" for k,v in sorted(c.items())))
except Exception as e:
    print(f"{sys.argv[1]}\tERR\t-\t-\t{e}")
PYEOF
sudo -n docker cp /tmp/scoreparse.py nmos-score:/tmp/scoreparse.py
: > /tmp/score.tsv
run(){
  echo "$(date +%T) running $1 vs $2:$3 $4" >> /tmp/score.log
  sudo -n docker exec -w /home/nmos-testing nmos-score bash -lc "timeout 260 python3 nmos-test.py suite $1 --host $2 --port $3 --version $4 --selection all --output /tmp/$1.json >/tmp/$1.out 2>&1"
  sudo -n docker exec nmos-score python3 /tmp/scoreparse.py "$1" >> /tmp/score.tsv 2>/dev/null
}
run IS-05-01 192.168.4.85 8107 v1.1
run IS-08-01 192.168.4.85 8094 v1.0
run IS-09-01 192.168.4.85 8080 v1.0
run IS-07-01 192.168.4.85 8102 v1.0
run IS-04-03 192.168.4.85 8107 v1.3
sudo -n docker rm -f nmos-score >/dev/null 2>&1
echo "ALL DONE" >> /tmp/score.tsv

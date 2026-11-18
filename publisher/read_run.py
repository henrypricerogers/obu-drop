"""Fetch a marketing-team run's results from its log: python3 publisher/read_run.py <run_id> > out.json"""
import base64, json, re, subprocess, sys, zlib
log = subprocess.run(["gh", "run", "view", sys.argv[1], "--repo", "henrypricerogers/obu-drop", "--log"], capture_output=True, text=True).stdout
parts = sorted((int(m.group(1)), m.group(2)) for m in re.finditer(r"TEAMDATA (\d{4}) (\S+)", log))
print(json.dumps(json.loads(zlib.decompress(base64.b64decode("".join(p for _, p in parts)))), indent=1))

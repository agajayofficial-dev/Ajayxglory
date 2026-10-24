import argparse
import json
from gen import generate_accounts

parser = argparse.ArgumentParser()
parser.add_argument("--total", type=int, default=4)
parser.add_argument("--threads", type=int, default=4)
parser.add_argument("--fresh", action="store_true")
args = parser.parse_args()
result = generate_accounts(args.total, args.threads, args.fresh)
print(json.dumps(result), flush=True)
if int(result.get("created", 0)) < args.total:
    raise SystemExit(2)

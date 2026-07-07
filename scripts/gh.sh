#!/usr/bin/env bash
# gh.sh — lean GitHub API helper for JhnsonO/ffa-automations
# Token comes from env: export GH_PAT=... (never hardcode, commit, or log it)
# Usage:
#   gh.sh get <repo_path> [ref]                      # print raw file
#   gh.sh push <local_file> <repo_path> "<msg>" [br] # create/update file (auto SHA)
#   gh.sh dispatch <workflow.yml> [ref] ['{json}']   # dispatch + print new run id
#   gh.sh latest [workflow.yml] [n]                  # compact recent runs
#   gh.sh run <run_id>                               # run + job/step conclusions
#   gh.sh logs <run_id> [context_lines]              # failed-job logs, error window only
#   gh.sh grep-log <run_id> <pattern>                # any job's log, matching lines only
#   gh.sh artifact <artifact_id> <out.zip>           # download artifact (redirect-safe)
#   gh.sh artifacts <run_id>                         # list artifacts for a run
#   gh.sh issue create "<title>" <body_file>         # open a GitHub issue
#   gh.sh issue list                                 # list open issues
#   gh.sh issue close <num> [comment_file]           # comment (optional) + close issue
#   gh.sh issue comment <num> <body_file>            # add a comment to an issue
#   gh.sh issue comments <num>                       # list comments on an issue
set -euo pipefail

REPO="JhnsonO/ffa-automations"
API="https://api.github.com/repos/${REPO}"
TOKEN="${GH_PAT:-${GITHUB_TOKEN:-}}"
[ -n "$TOKEN" ] || { echo "ERROR: set GH_PAT env var" >&2; exit 1; }

auth=(-H "Authorization: token ${TOKEN}")
json=(-H "Accept: application/vnd.github.v3+json")
raw=(-H "Accept: application/vnd.github.v3.raw")

cmd="${1:-}"; shift || true

case "$cmd" in

get)
  path="${1:?repo_path}"; ref="${2:-main}"
  curl -sf "${auth[@]}" "${raw[@]}" "${API}/contents/${path}?ref=${ref}"
  ;;

push)
  local_file="${1:?local_file}"; repo_path="${2:?repo_path}"
  msg="${3:?commit message}"; branch="${4:-main}"
  sha=$(curl -s "${auth[@]}" "${json[@]}" "${API}/contents/${repo_path}?ref=${branch}" \
    | python3 -c "import json,sys
try: print(json.load(sys.stdin).get('sha',''))
except Exception: print('')")
  base64 -w0 "$local_file" > /tmp/.gh_b64
  payload=$(python3 - "$msg" "$branch" "$sha" <<'PY'
import json,sys
msg,branch,sha=sys.argv[1],sys.argv[2],sys.argv[3]
d={"message":msg,"branch":branch,"content":open("/tmp/.gh_b64").read().strip()}
if sha: d["sha"]=sha
print(json.dumps(d))
PY
  )
  curl -sf -X PUT "${auth[@]}" -H "Content-Type: application/json" \
    "${API}/contents/${repo_path}" -d "$payload" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('commit', d['commit']['sha'][:7], d['content']['path'])" \
    || { echo "ERROR: push failed for ${repo_path}" >&2; exit 1; }
  rm -f /tmp/.gh_b64
  ;;

dispatch)
  wf="${1:?workflow file}"; ref="${2:-main}"; inputs="${3:-{}}"
  code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${auth[@]}" "${json[@]}" \
    "${API}/actions/workflows/${wf}/dispatches" \
    -d "{\"ref\":\"${ref}\",\"inputs\":${inputs}}")
  [ "$code" = "204" ] || { echo "ERROR: dispatch HTTP ${code}" >&2; exit 1; }
  sleep 6
  curl -s "${auth[@]}" "${API}/actions/workflows/${wf}/runs?per_page=1" \
    | python3 -c "import json,sys; r=json.load(sys.stdin)['workflow_runs'][0]; print('dispatched run', r['id'], r['status'])"
  ;;

latest)
  wf="${1:-}"; n="${2:-5}"
  url="${API}/actions/runs?per_page=${n}"
  [ -n "$wf" ] && url="${API}/actions/workflows/${wf}/runs?per_page=${n}"
  curl -s "${auth[@]}" "$url" | python3 -c "
import json,sys
for r in json.load(sys.stdin)['workflow_runs']:
    print(r['id'], r['name'][:30], r['head_branch'], r['status'], r['conclusion'], r['created_at'])"
  ;;

run)
  rid="${1:?run_id}"
  curl -s "${auth[@]}" "${API}/actions/runs/${rid}/jobs" | python3 -c "
import json,sys
for j in json.load(sys.stdin)['jobs']:
    print(f\"JOB {j['name']}: {j['status']}/{j['conclusion']} (id {j['id']})\")
    for s in j['steps']:
        if s['conclusion'] not in ('success','skipped',None):
            print(f\"  FAILED STEP: {s['name']} -> {s['conclusion']}\")"
  ;;

logs)
  rid="${1:?run_id}"; ctx="${2:-4}"
  # find failed job(s), fetch logs, strip ANSI, print only error windows
  jobs=$(curl -s "${auth[@]}" "${API}/actions/runs/${rid}/jobs" \
    | python3 -c "import json,sys; print(' '.join(str(j['id']) for j in json.load(sys.stdin)['jobs'] if j['conclusion'] not in ('success','skipped')))")
  [ -n "$jobs" ] || { echo "no failed jobs"; exit 0; }
  for jid in $jobs; do
    echo "===== job ${jid} ====="
    curl -sL "${auth[@]}" "${API}/actions/jobs/${jid}/logs" | python3 -c "
import re,sys
ansi=re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
noise=re.compile(r'(fps=|speed=|frame=|size=|bitrate=|Get:|Reading |Preparing |Unpacking |Setting up |Selecting )')
keys=re.compile(r'(ERROR|error:|##\[error\]|Traceback|Exception|FAILED|fatal:|No such file|not found|Unrecognized|refused|denied|timeout|Killed)', re.I)
lines=[ansi.sub('',l.rstrip()) for l in sys.stdin]
lines=[l for l in lines if not noise.search(l)]
ctx=int('${ctx}')
hits=set()
for i,l in enumerate(lines):
    if keys.search(l): hits.update(range(max(0,i-ctx), min(len(lines), i+ctx+1)))
if not hits:
    print('(no error-pattern lines; last 20 lines:)'); [print(l) for l in lines[-20:]]
else:
    prev=-2
    for i in sorted(hits):
        if i != prev+1: print('  ---')
        print(lines[i]); prev=i"
  done
  ;;

grep-log)
  rid="${1:?run_id}"; pat="${2:?grep_pattern}"
  # Unlike `logs`, works on any job regardless of conclusion. Only matching
  # lines are printed -- for pulling one dynamic value (e.g. a runtime URL)
  # out of a log without dumping the whole thing.
  jobs=$(curl -s "${auth[@]}" "${API}/actions/runs/${rid}/jobs" \
    | python3 -c "import json,sys; print(' '.join(str(j['id']) for j in json.load(sys.stdin)['jobs']))")
  for jid in $jobs; do
    curl -sL "${auth[@]}" "${API}/actions/jobs/${jid}/logs" | grep -oE "$pat" || true
  done
  ;;

artifacts)
  rid="${1:?run_id}"
  curl -s "${auth[@]}" "${API}/actions/runs/${rid}/artifacts" | python3 -c "
import json,sys
for a in json.load(sys.stdin)['artifacts']:
    print(a['id'], a['name'], f\"{a['size_in_bytes']/1e6:.1f}MB\", 'expired' if a['expired'] else 'ok')"
  ;;

artifact)
  aid="${1:?artifact_id}"; out="${2:?out.zip}"
  curl -sL "${auth[@]}" "${API}/actions/artifacts/${aid}/zip" -o "$out"
  ls -la "$out"
  ;;

issue)
  sub="${1:?create|list}"; shift || true
  case "$sub" in
  create)
    title="${1:?title}"; body_file="${2:?body_file}"
    payload=$(python3 - "$title" "$body_file" <<'PY'
import json,sys
print(json.dumps({"title":sys.argv[1],"body":open(sys.argv[2]).read()}))
PY
    )
    curl -sf -X POST "${auth[@]}" "${json[@]}" "${API}/issues" -d "$payload" \
      | python3 -c "import json,sys; d=json.load(sys.stdin); print('issue #%d %s' % (d['number'], d['html_url']))"
    ;;
  comment)
    num="${1:?issue_number}"; body_file="${2:?body_file}"
    payload=$(python3 - "$body_file" <<'PY2'
import json,sys
print(json.dumps({"body":open(sys.argv[1]).read()}))
PY2
    )
    curl -sf -X POST "${auth[@]}" "${json[@]}" "${API}/issues/${num}/comments" -d "$payload" \
      | python3 -c "import json,sys; d=json.load(sys.stdin); print('comment on #' + d['html_url'].split('/issues/')[1].split('#')[0])"
    ;;
  comments)
    num="${1:?issue_number}"
    curl -sf "${auth[@]}" "${json[@]}" "${API}/issues/${num}/comments?per_page=100" \
      | python3 -c "
import json,sys
for c in json.load(sys.stdin):
    print('----- comment id %s by %s at %s -----' % (c['id'], c['user']['login'], c['created_at']))
    print(c['body'])
    print()"
    ;;

  close)
    num="${1:?issue_number}"; body_file="${2:-}"
    if [ -n "$body_file" ]; then
      payload=$(python3 - "$body_file" <<'PY'
import json,sys
print(json.dumps({"body":open(sys.argv[1]).read()}))
PY
      )
      curl -sf -X POST "${auth[@]}" "${json[@]}" "${API}/issues/${num}/comments" -d "$payload" >/dev/null
    fi
    curl -sf -X PATCH "${auth[@]}" "${json[@]}" "${API}/issues/${num}" -d '{"state":"closed"}' \
      | python3 -c "import json,sys; d=json.load(sys.stdin); print('issue #%d %s' % (d['number'], d['state']))"
    ;;
  list)
    curl -s "${auth[@]}" "${json[@]}" "${API}/issues?state=open&per_page=30" | python3 -c "
import json,sys
for i in json.load(sys.stdin):
    if 'pull_request' in i: continue
    print('#%d %s' % (i['number'], i['title']))"
    ;;
  *) echo "usage: gh.sh issue create '<title>' <body_file> | close <num> [comment_file] | list | comments <num> | comment <num> <body_file>" >&2; exit 1;;
  esac
  ;;

*)
  grep '^#   gh.sh' "$0" | sed 's/^#   //'
  exit 1
  ;;
esac

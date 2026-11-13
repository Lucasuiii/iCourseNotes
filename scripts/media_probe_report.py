"""Merge probe artifacts into one timeline relative to the first session start (numbers only)."""
import glob
import json
import sys


def main(folder):
    runs = [json.loads(open(p).read()) for p in sorted(glob.glob(f'{folder}/**/*.json', recursive=True))]
    if not runs:
        print('No probe results'); return
    t0 = min(r['start_at'] for r in runs)
    logins = sorted((s['login']['at'] - t0, s['label']) for r in runs for s in r['sessions'] if s.get('login', {}).get('ok'))
    print('logins (s from start):', [(round(t, 1), label) for t, label in logins])
    for r in runs:
        for s in r['sessions']:
            bad = [(round(e['at'] - t0, 1), e.get('status', e.get('error')), e.get('redirect'))
                   for e in s.get('events', []) if e['kind'] == 'media' and e.get('status') != 206]
            api_bad = [round(e['at'] - t0, 1) for e in s.get('events', []) if e['kind'] == 'api' and not e['ok']]
            ok = sum(e.get('status') == 206 for e in s.get('events', []) if e['kind'] == 'media')
            print(f"{s.get('label')}: login={s.get('login')} 206={ok} non206={bad[:20]} api_fail_at={api_bad[:20]}")


if __name__ == '__main__':
    main(sys.argv[1])

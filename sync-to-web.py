#!/usr/bin/env python3
"""
Sync today's push content to the daily-push website.
Called by push-agent after each daily push.

Usage: python3 sync-to-web.py '{"date":"2026-03-14","weekday":"周六","briefing":{...},"dedao":[...]}'
   or: python3 sync-to-web.py --from-file /path/to/data.json

The JSON should match the posts/YYYY-MM-DD.json schema.
"""
import json, sys, os, subprocess, glob

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
POSTS_DIR = os.path.join(PROJECT_DIR, 'posts')

def save_post(data):
    date = data.get('date')
    if not date:
        print('❌ Missing "date" field')
        sys.exit(1)
    
    # Write post file
    post_file = os.path.join(POSTS_DIR, f'{date}.json')
    with open(post_file, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f'✅ Saved {post_file}')
    
    # Update index.json
    index_file = os.path.join(POSTS_DIR, 'index.json')
    if os.path.exists(index_file):
        with open(index_file) as f:
            dates = json.load(f)
    else:
        dates = []
    
    if date not in dates:
        dates.append(date)
        dates.sort()
    
    with open(index_file, 'w') as f:
        json.dump(dates, f, ensure_ascii=False)
    print(f'✅ Updated index.json ({len(dates)} dates)')
    
    # Update data.json (all posts combined)
    all_posts = []
    for pf in sorted(glob.glob(os.path.join(POSTS_DIR, '2026-*.json'))):
        with open(pf) as f:
            all_posts.append(json.load(f))
    
    data_file = os.path.join(PROJECT_DIR, 'data.json')
    with open(data_file, 'w') as f:
        json.dump(all_posts, f, indent=2, ensure_ascii=False)
    print(f'✅ Updated data.json ({len(all_posts)} days)')
    
    # Git commit and push
    os.chdir(PROJECT_DIR)
    subprocess.run(['git', 'add', '-A'], check=True)
    result = subprocess.run(['git', 'diff', '--cached', '--quiet'])
    if result.returncode != 0:
        subprocess.run(['git', 'commit', '-m', f'📡 Daily push {date}'], check=True)
        subprocess.run(['git', 'push'], check=True)
        print(f'✅ Pushed to GitHub')
    else:
        print('ℹ️ No changes to push')

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python3 sync-to-web.py \'{"date":"...","briefing":{...}}\'')
        print('   or: python3 sync-to-web.py --from-file /path/to/data.json')
        sys.exit(1)
    
    if sys.argv[1] == '--from-file':
        with open(sys.argv[2]) as f:
            data = json.load(f)
    else:
        data = json.loads(sys.argv[1])
    
    save_post(data)

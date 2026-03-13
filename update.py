#!/usr/bin/env python3
"""
Extract push content from push-agent sessions and update data.json.
Run after each daily push to keep the web page in sync.
Usage: python3 update.py
"""
import json, glob, os, re, subprocess

SESSIONS_DIR = os.path.expanduser('~/.openclaw/agents/push-agent/sessions')
DATA_FILE = os.path.join(os.path.dirname(__file__), 'data.json')
PROJECT_DIR = os.path.dirname(__file__)

def extract_messages():
    """Extract all sent messages from push-agent sessions."""
    files = glob.glob(os.path.join(SESSIONS_DIR, '*.jsonl'))
    messages = []
    for f in sorted(files, key=os.path.getmtime):
        with open(f) as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                    if obj.get('type') == 'message':
                        ts = obj.get('timestamp', '')
                        for c in obj['message'].get('content', []):
                            if isinstance(c, dict) and c.get('type') == 'toolCall' and c.get('name') == 'message':
                                args = c.get('arguments', {})
                                m = args.get('message', '')
                                if len(m) > 200:
                                    messages.append({'timestamp': ts, 'message': m})
                except:
                    pass
    return messages

def git_push():
    """Commit and push to GitHub."""
    os.chdir(PROJECT_DIR)
    subprocess.run(['git', 'add', 'data.json'], check=True)
    result = subprocess.run(['git', 'diff', '--cached', '--quiet'])
    if result.returncode != 0:  # There are changes
        subprocess.run(['git', 'commit', '-m', '📡 Update daily push data'], check=True)
        subprocess.run(['git', 'push'], check=True)
        print('✅ Pushed to GitHub')
    else:
        print('ℹ️ No changes to push')

if __name__ == '__main__':
    msgs = extract_messages()
    print(f'Found {len(msgs)} messages from push-agent')
    for m in msgs:
        print(f"  [{m['timestamp'][:10]}] {m['message'][:60]}...")
    
    # For now, just report. Full parsing can be added later.
    # The main agent updates data.json directly after each push.
    git_push()

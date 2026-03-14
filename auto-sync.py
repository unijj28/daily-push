#!/usr/bin/env python3
"""
Auto-sync: Extract today's briefing + dedao analysis from push-agent sessions
and publish to the daily-push website.

Runs daily at 9:30 AM via cron (after briefing@8:00 + dedao@9:00).

Data sources:
  - Briefing: push-agent session messages containing "每日简报"
  - Dedao: ~/Projects/dedao-daily/posts/YYYY-MM-DD.json (if exists)
"""
import json, glob, os, subprocess, re
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
POSTS_DIR = os.path.join(PROJECT_DIR, 'posts')
SESSIONS_DIR = os.path.expanduser('~/.openclaw/agents/push-agent/sessions')
DEDAO_DIR = os.path.expanduser('~/Projects/dedao-daily/posts')

def get_today():
    return datetime.now().strftime('%Y-%m-%d')

def get_weekday():
    days = ['周一','周二','周三','周四','周五','周六','周日']
    return days[datetime.now().weekday()]

# ─── Extract briefing from push-agent sessions ───

def extract_briefing_text(date):
    """Find the briefing message from push-agent sessions for given date."""
    files = glob.glob(os.path.join(SESSIONS_DIR, '*.jsonl'))
    briefing = None
    for f in sorted(files, key=os.path.getmtime, reverse=True):
        with open(f) as fh:
            for line in fh:
                if date not in line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get('type') != 'message':
                        continue
                    for c in obj['message'].get('content', []):
                        if (isinstance(c, dict) and c.get('type') == 'toolCall' 
                            and c.get('name') == 'message'):
                            m = c.get('arguments', {}).get('message', '')
                            if len(m) > 500 and '每日简报' in m:
                                briefing = m
                except:
                    pass
        if briefing:
            break
    return briefing

# ─── Parse briefing text into structured data ───

def parse_section(text, header_pattern):
    match = re.search(header_pattern, text)
    if not match:
        return []
    start = match.end()
    items = []
    for line in text[start:].split('\n'):
        line = line.strip()
        if line.startswith('•') or line.startswith('-'):
            items.append(re.sub(r'^[•\-]\s*', '', line))
        elif line and re.match(r'^[🇦🇺🌏🇨🇳🤖🌤📈📡]', line):
            break
    return items

def parse_ai_items(text):
    section = re.search(r'🤖.*?(?:技术动态|AI)', text)
    if not section:
        return []
    start = section.end()
    items = []
    for line in text[start:].split('\n'):
        line = line.strip()
        if line.startswith('•') or line.startswith('-'):
            content = re.sub(r'^[•\-]\s*', '', line)
            # Try to split title:comment on first colon
            parts = content.split('：', 1)
            if len(parts) == 2:
                items.append({"title": parts[0], "comment": parts[1]})
            else:
                items.append({"title": content, "comment": ""})
        elif line and re.match(r'^[🌤📈📡]', line):
            break
    return items

def parse_weather(text):
    temp_low, temp_high, humidity = 0, 0, 0
    condition, wind, tip = "", "", ""
    
    weather_match = re.search(r'🌤.*天气(.*?)(?=📈|$)', text, re.DOTALL)
    if weather_match:
        wtext = weather_match.group(1)
        temps = re.findall(r'(\d+)°C', wtext)
        if len(temps) >= 2:
            temp_low = int(min(temps, key=int))
            temp_high = int(max(temps, key=int))
        hum = re.search(r'湿度[约]?(\d+)%', wtext)
        if hum:
            humidity = int(hum.group(1))
        lines = [l.strip() for l in wtext.strip().split('\n') if l.strip().startswith('•')]
        if lines:
            first = re.sub(r'^[•\-]\s*', '', lines[0])
            condition = first.split('，')[0] if '，' in first else first[:30]
        tip_match = re.search(r'[💡☂️🌂提示][:：]?\s*(.*)', wtext)
        if tip_match:
            tip = tip_match.group(1).strip()
    
    return {
        "condition": condition or "详见简报",
        "tempLow": temp_low, "tempHigh": temp_high,
        "humidity": humidity, "wind": wind or "详见简报",
        "tip": tip
    }

def parse_markets(text):
    markets = {}
    market_section = re.search(r'📈.*?(?=📡|$)', text, re.DOTALL)
    if not market_section:
        return markets
    mtext = market_section.group(0)
    
    patterns = [
        ('asx200', r'ASX\s*200[:\s]*([0-9,]+)\s*[点]?.*?([+-]?\d+\.?\d*%)'),
        ('sp500', r'标普500[:\s]*([0-9,]+)\s*[点]?.*?([+-]?\d+\.?\d*%)'),
        ('dow', r'道琼斯[:\s]*([0-9,]+)\s*[点]?.*?([+-]?\d+\.?\d*%)'),
        ('gold', r'黄金[:\s]*[~约]?\$?([0-9,]+).*?/(?:盎司|oz)'),
        ('iron', r'铁矿石[:\s]*[~约]?\$?([0-9,.\-]+).*?/(?:吨|t)'),
        ('oil', r'(?:原油|布伦特)[:\s]*[~约]?\$?([0-9,.]+).*?/(?:桶|bbl)'),
    ]
    
    for key, pat in patterns:
        m = re.search(pat, mtext)
        if m:
            value = m.group(1)
            change = m.group(2) if m.lastindex >= 2 else ""
            if key == 'gold': value = f'${value}/oz'
            elif key == 'iron': value = f'${value}/t'
            elif key == 'oil': value = f'${value}/bbl'
            markets[key] = {"value": value, "change": change}
    
    return markets

# ─── Load dedao analysis ───

def load_dedao(date):
    """Load dedao analysis from dedao-daily project if available."""
    dedao_file = os.path.join(DEDAO_DIR, f'{date}.json')
    if not os.path.exists(dedao_file):
        return []
    
    with open(dedao_file) as f:
        data = json.load(f)
    
    articles = data.get('articles', [])
    result = []
    for a in articles:
        result.append({
            "course": a.get('course', ''),
            "episode": a.get('number', 0),
            "title": a.get('title', ''),
            "status": "已分析",
            "analysis": {
                "summary": a.get('summary', ''),
                "views": a.get('views', []),
                "strengths": a.get('analysis', {}).get('strengths', ''),
                "weaknesses": a.get('analysis', {}).get('weaknesses', ''),
                "bias": a.get('analysis', {}).get('bias', ''),
                "inspiration": a.get('inspiration', {})
            }
        })
    return result

# ─── Build and publish ───

def build_post(date, briefing_text):
    dedao = load_dedao(date)
    
    return {
        "date": date,
        "weekday": get_weekday(),
        "briefing": {
            "australia": parse_section(briefing_text, r'🇦🇺.*?要闻\n?'),
            "world": parse_section(briefing_text, r'🌏.*?头条\n?'),
            "china": parse_section(briefing_text, r'🇨🇳.*?新闻\n?'),
            "ai": parse_ai_items(briefing_text),
            "weather": parse_weather(briefing_text),
            "markets": parse_markets(briefing_text)
        },
        "dedao": dedao
    }

def save_and_push(post):
    date = post['date']
    os.makedirs(POSTS_DIR, exist_ok=True)
    
    # Save post
    with open(os.path.join(POSTS_DIR, f'{date}.json'), 'w') as f:
        json.dump(post, f, indent=2, ensure_ascii=False)
    print(f'✅ Saved posts/{date}.json')
    
    # Update index
    dates = sorted([fn.replace('.json','') for fn in os.listdir(POSTS_DIR) 
                    if fn.startswith('20') and fn.endswith('.json')])
    with open(os.path.join(POSTS_DIR, 'index.json'), 'w') as f:
        json.dump(dates, f, ensure_ascii=False)
    
    # Update data.json
    all_posts = []
    for d in dates:
        with open(os.path.join(POSTS_DIR, f'{d}.json')) as f:
            all_posts.append(json.load(f))
    with open(os.path.join(PROJECT_DIR, 'data.json'), 'w') as f:
        json.dump(all_posts, f, indent=2, ensure_ascii=False)
    print(f'✅ Updated index + data.json ({len(dates)} days)')
    
    # Git push
    os.chdir(PROJECT_DIR)
    subprocess.run(['git', 'add', '-A'], check=True)
    result = subprocess.run(['git', 'diff', '--cached', '--quiet'])
    if result.returncode != 0:
        subprocess.run(['git', 'commit', '-m', f'📡 Daily push {date}'], check=True)
        subprocess.run(['git', 'push'], check=True)
        print(f'✅ Pushed to GitHub')
    else:
        print('ℹ️ Already up to date')

if __name__ == '__main__':
    date = get_today()
    post_file = os.path.join(POSTS_DIR, f'{date}.json')
    
    if os.path.exists(post_file):
        print(f'ℹ️ {date} already synced, skipping')
        exit(0)
    
    print(f'🔍 Looking for {date} briefing...')
    text = extract_briefing_text(date)
    if not text:
        print(f'❌ No briefing found for {date}')
        exit(1)
    
    print(f'📝 Parsing briefing ({len(text)} chars)...')
    print(f'📚 Loading dedao analysis...')
    post = build_post(date, text)
    
    dedao_count = len(post['dedao'])
    print(f'   Found {dedao_count} dedao articles')
    
    save_and_push(post)
    print(f'🎉 Done! {date} is now on the website.')

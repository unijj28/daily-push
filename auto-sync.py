#!/usr/bin/env python3
"""
Auto-sync: extract a daily briefing + dedao analysis, publish to the daily-push
website, and optionally archive the same content into Notion.

Default flow:
  1) Extract briefing text from push-agent sessions
  2) Load dedao analysis from ~/Projects/dedao-daily/posts/YYYY-MM-DD.json
  3) Save posts/YYYY-MM-DD.json + update index/data.json + git push
  4) Sync the same post into Notion archive database (if configured)

Notion env vars:
  - NOTION_API_KEY
  - NOTION_ARCHIVE_DATABASE_ID
  - NOTION_ARCHIVE_PARENT_PAGE_ID   (optional; can bootstrap an archive page+db)

Examples:
  python3 auto-sync.py
  python3 auto-sync.py --date 2026-04-03
  python3 auto-sync.py --skip-notion
"""
import argparse
import glob
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
POSTS_DIR = os.path.join(PROJECT_DIR, 'posts')
SESSIONS_DIR = os.path.expanduser('~/.openclaw/agents/push-agent/sessions')
DEDAO_DIR = os.path.expanduser('~/Projects/dedao-daily/posts')
NOTION_STATE_FILE = os.path.join(PROJECT_DIR, '.notion-archive.json')
NOTION_VERSION = '2022-06-28'
NOTION_API_BASE = 'https://api.notion.com/v1'


def get_today():
    return datetime.now().strftime('%Y-%m-%d')


def get_weekday(date_str=None):
    days = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    dt = datetime.strptime(date_str, '%Y-%m-%d') if date_str else datetime.now()
    return days[dt.weekday()]


# ─── Extract briefing from push-agent sessions ───────────────────────────────

def extract_briefing_text(date):
    """Find the briefing message from push-agent sessions for given date.

    Sessions are timestamped in UTC, but briefings are sent at 8am Melbourne time
    (= previous UTC day in summer). Search by Chinese date in content instead.
    """
    dt = datetime.strptime(date, '%Y-%m-%d')
    chinese_date = f'{dt.year}年{dt.month}月{dt.day}日'

    files = glob.glob(os.path.join(SESSIONS_DIR, '*.jsonl'))
    briefing = None
    for f in sorted(files, key=os.path.getmtime, reverse=True):
        with open(f, encoding='utf-8') as fh:
            for line in fh:
                if '每日简报' not in line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get('type') != 'message':
                        continue
                    for c in obj['message'].get('content', []):
                        if (
                            isinstance(c, dict)
                            and c.get('type') == 'toolCall'
                            and c.get('name') == 'message'
                        ):
                            m = c.get('arguments', {}).get('message', '')
                            if len(m) > 500 and '每日简报' in m and chinese_date in m:
                                briefing = m
                except Exception:
                    pass
        if briefing:
            break
    return briefing


# ─── Parse briefing text into structured data ────────────────────────────────

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
            parts = content.split('：', 1)
            if len(parts) == 2:
                items.append({'title': parts[0], 'comment': parts[1]})
            else:
                items.append({'title': content, 'comment': ''})
        elif line and re.match(r'^[🌤📈📡]', line):
            break
    return items


def parse_weather(text):
    temp_low, temp_high, humidity = 0, 0, 0
    condition, wind, tip = '', '', ''

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
        'condition': condition or '详见简报',
        'tempLow': temp_low,
        'tempHigh': temp_high,
        'humidity': humidity,
        'wind': wind or '详见简报',
        'tip': tip,
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
            change = m.group(2) if m.lastindex and m.lastindex >= 2 else ''
            if key == 'gold':
                value = f'${value}/oz'
            elif key == 'iron':
                value = f'${value}/t'
            elif key == 'oil':
                value = f'${value}/bbl'
            markets[key] = {'value': value, 'change': change}

    return markets


# ─── Load dedao analysis ──────────────────────────────────────────────────────

def load_dedao(date):
    dedao_file = os.path.join(DEDAO_DIR, f'{date}.json')
    if not os.path.exists(dedao_file):
        return []

    with open(dedao_file, encoding='utf-8') as f:
        data = json.load(f)

    articles = data.get('articles', [])
    result = []
    for a in articles:
        result.append({
            'course': a.get('course', ''),
            'episode': a.get('number', 0),
            'title': a.get('title', ''),
            'status': '已分析',
            'analysis': {
                'summary': a.get('summary', ''),
                'views': a.get('views', []),
                'strengths': a.get('analysis', {}).get('strengths', ''),
                'weaknesses': a.get('analysis', {}).get('weaknesses', ''),
                'bias': a.get('analysis', {}).get('bias', ''),
                'inspiration': a.get('inspiration', {}),
            },
        })
    return result


# ─── Build and publish ────────────────────────────────────────────────────────

def build_post(date, briefing_text):
    dedao = load_dedao(date)
    return {
        'date': date,
        'weekday': get_weekday(date),
        'briefingText': briefing_text,
        'briefing': {
            'australia': parse_section(briefing_text, r'🇦🇺.*?要闻\n?'),
            'world': parse_section(briefing_text, r'🌏.*?头条\n?'),
            'china': parse_section(briefing_text, r'🇨🇳.*?新闻\n?'),
            'ai': parse_ai_items(briefing_text),
            'weather': parse_weather(briefing_text),
            'markets': parse_markets(briefing_text),
        },
        'dedao': dedao,
    }


def save_and_push(post):
    date = post['date']
    os.makedirs(POSTS_DIR, exist_ok=True)

    with open(os.path.join(POSTS_DIR, f'{date}.json'), 'w', encoding='utf-8') as f:
        json.dump(post, f, indent=2, ensure_ascii=False)
    print(f'✅ Saved posts/{date}.json')

    dates = sorted([
        fn.replace('.json', '')
        for fn in os.listdir(POSTS_DIR)
        if fn.startswith('20') and fn.endswith('.json')
    ])
    with open(os.path.join(POSTS_DIR, 'index.json'), 'w', encoding='utf-8') as f:
        json.dump(dates, f, ensure_ascii=False)

    all_posts = []
    for d in dates:
        with open(os.path.join(POSTS_DIR, f'{d}.json'), encoding='utf-8') as f:
            all_posts.append(json.load(f))
    with open(os.path.join(PROJECT_DIR, 'data.json'), 'w', encoding='utf-8') as f:
        json.dump(all_posts, f, indent=2, ensure_ascii=False)
    print(f'✅ Updated index + data.json ({len(dates)} days)')

    os.chdir(PROJECT_DIR)
    subprocess.run(['git', 'add', '-A'], check=True)
    result = subprocess.run(['git', 'diff', '--cached', '--quiet'])
    if result.returncode != 0:
        subprocess.run(['git', 'commit', '-m', f'📡 Daily push {date}'], check=True)
        subprocess.run(['git', 'push'], check=True)
        print('✅ Pushed to GitHub')
    else:
        print('ℹ️ Already up to date')


# ─── Notion sync ──────────────────────────────────────────────────────────────

def load_notion_state():
    if not os.path.exists(NOTION_STATE_FILE):
        return {}
    try:
        with open(NOTION_STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_notion_state(state):
    with open(NOTION_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def notion_headers(api_key):
    return {
        'Authorization': f'Bearer {api_key}',
        'Notion-Version': NOTION_VERSION,
        'Content-Type': 'application/json',
    }


def notion_request(method, path, api_key, payload=None):
    url = f'{NOTION_API_BASE}{path}'
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=body, method=method, headers=notion_headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f'Notion API {method} {path} failed: {e.code} {detail[:400]}') from e


def split_text(text, limit=1800):
    text = (text or '').strip()
    if not text:
        return []
    chunks = []
    current = ''
    for part in text.split('\n'):
        candidate = f'{current}\n{part}'.strip() if current else part
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            while len(part) > limit:
                chunks.append(part[:limit])
                part = part[limit:]
            current = part
    if current:
        chunks.append(current)
    return chunks


def paragraph_block(text):
    return {
        'object': 'block',
        'type': 'paragraph',
        'paragraph': {
            'rich_text': [{'type': 'text', 'text': {'content': text[:2000]}}],
        },
    }


def heading_block(level, text):
    key = f'heading_{level}'
    return {
        'object': 'block',
        'type': key,
        key: {'rich_text': [{'type': 'text', 'text': {'content': text[:2000]}}]},
    }


def bullet_block(text):
    return {
        'object': 'block',
        'type': 'bulleted_list_item',
        'bulleted_list_item': {
            'rich_text': [{'type': 'text', 'text': {'content': text[:2000]}}],
        },
    }


def code_block(text, language='json'):
    return {
        'object': 'block',
        'type': 'code',
        'code': {
            'rich_text': [{'type': 'text', 'text': {'content': text[:2000]}}],
            'language': language,
        },
    }


def children_batches(children, batch_size=100):
    for i in range(0, len(children), batch_size):
        yield children[i:i + batch_size]


def build_summary(post):
    briefing = post.get('briefing', {})
    dedao = post.get('dedao', [])
    parts = [
        f"澳洲 {len(briefing.get('australia', []))} 条",
        f"国际 {len(briefing.get('world', []))} 条",
        f"中国 {len(briefing.get('china', []))} 条",
        f"AI {len(briefing.get('ai', []))} 条",
    ]
    if dedao:
        parts.append(f'得到 {len(dedao)} 篇')
    return ' / '.join(parts)


def build_notion_children(post):
    briefing = post.get('briefing', {})
    dedao = post.get('dedao', [])
    children = [
        heading_block(1, f"{post['date']} {post['weekday']} 内容归档"),
        paragraph_block(build_summary(post)),
        heading_block(2, '每日简报原文'),
    ]

    for chunk in split_text(post.get('briefingText', '')):
        children.append(paragraph_block(chunk))

    section_map = [
        ('澳洲要闻', briefing.get('australia', [])),
        ('国际头条', briefing.get('world', [])),
        ('中国新闻', briefing.get('china', [])),
    ]
    for title, items in section_map:
        children.append(heading_block(2, title))
        if items:
            for item in items:
                children.append(bullet_block(item))
        else:
            children.append(paragraph_block('无'))

    children.append(heading_block(2, 'AI / LLM 技术动态'))
    ai_items = briefing.get('ai', [])
    if ai_items:
        for item in ai_items:
            line = item.get('title', '')
            if item.get('comment'):
                line = f"{line} —— {item['comment']}"
            children.append(bullet_block(line))
    else:
        children.append(paragraph_block('无'))

    children.append(heading_block(2, '天气'))
    weather = briefing.get('weather', {})
    weather_lines = [
        f"天气：{weather.get('condition', '详见简报')}",
        f"温度：{weather.get('tempLow', 0)}°C - {weather.get('tempHigh', 0)}°C",
        f"湿度：{weather.get('humidity', 0)}%",
    ]
    if weather.get('tip'):
        weather_lines.append(f"提示：{weather['tip']}")
    for line in weather_lines:
        children.append(bullet_block(line))

    children.append(heading_block(2, '市场'))
    markets = briefing.get('markets', {})
    if markets:
        for key, value in markets.items():
            line = f"{key}: {value.get('value', '')}"
            if value.get('change'):
                line += f" ({value['change']})"
            children.append(bullet_block(line))
    else:
        children.append(paragraph_block('无'))

    children.append(heading_block(2, '得到分析'))
    if dedao:
        for article in dedao:
            title = f"{article.get('course', '')} #{article.get('episode', '')} {article.get('title', '')}".strip()
            children.append(heading_block(3, title[:2000] or '未命名文章'))
            summary = article.get('analysis', {}).get('summary') or article.get('analysis', {}).get('strengths') or ''
            if summary:
                for chunk in split_text(summary):
                    children.append(paragraph_block(chunk))
            views = article.get('analysis', {}).get('views', []) or []
            for view in views[:10]:
                children.append(bullet_block(str(view)))
    else:
        children.append(paragraph_block('今日无得到分析'))

    children.append(heading_block(2, '结构化 JSON'))
    structured = json.dumps(post, ensure_ascii=False, indent=2)
    for chunk in split_text(structured, limit=1800):
        children.append(code_block(chunk, language='json'))
    return children


def archive_page_title():
    return '内容归档 Archive'


def create_archive_page(api_key, parent_page_id):
    payload = {
        'parent': {'type': 'page_id', 'page_id': parent_page_id},
        'properties': {
            'title': {
                'title': [{'type': 'text', 'text': {'content': archive_page_title()}}],
            }
        },
        'children': [
            heading_block(1, archive_page_title()),
            paragraph_block('自动归档入口页。数据库做数据底座；页面视图/分类做展示层。'),
        ],
    }
    return notion_request('POST', '/pages', api_key, payload)


def create_archive_database(api_key, parent_page_id):
    payload = {
        'parent': {'type': 'page_id', 'page_id': parent_page_id},
        'title': [{'type': 'text', 'text': {'content': 'Content Archive Database'}}],
        'properties': {
            'Name': {'title': {}},
            'Date': {'date': {}},
            'Type': {'select': {'options': []}},
            'Category': {'multi_select': {'options': []}},
            'Source': {'rich_text': {}},
            'Status': {'select': {'options': []}},
            'Platform': {'multi_select': {'options': []}},
            'Tags': {'multi_select': {'options': []}},
            'Summary': {'rich_text': {}},
        },
    }
    return notion_request('POST', '/databases', api_key, payload)


def resolve_notion_targets(api_key):
    state = load_notion_state()
    database_id = (os.getenv('NOTION_ARCHIVE_DATABASE_ID') or state.get('database_id') or '').strip()
    archive_page_id = (os.getenv('NOTION_ARCHIVE_PAGE_ID') or state.get('archive_page_id') or '').strip()
    parent_page_id = (os.getenv('NOTION_ARCHIVE_PARENT_PAGE_ID') or '').strip()

    if database_id:
        return {'database_id': database_id, 'archive_page_id': archive_page_id}

    if not archive_page_id and not parent_page_id:
        return None

    print('🪄 Bootstrapping Notion archive page/database...')
    if not archive_page_id:
        archive_page = create_archive_page(api_key, parent_page_id)
        archive_page_id = archive_page['id']
        print(f'✅ Created archive page: {archive_page_title()}')

    database = create_archive_database(api_key, archive_page_id)
    database_id = database['id']
    state.update({'archive_page_id': archive_page_id, 'database_id': database_id})
    save_notion_state(state)
    print(f'✅ Created archive database: {database_id}')
    print(f'ℹ️ Saved Notion ids to {NOTION_STATE_FILE}')
    return {'database_id': database_id, 'archive_page_id': archive_page_id}


def notion_query_by_date(api_key, database_id, date):
    payload = {
        'filter': {
            'property': 'Date',
            'date': {'equals': date},
        },
        'page_size': 10,
    }
    res = notion_request('POST', f'/databases/{database_id}/query', api_key, payload)
    return res.get('results', [])


def notion_page_properties(post):
    date = post['date']
    summary = build_summary(post)
    tags = ['daily-push', 'briefing'] + (['dedao'] if post.get('dedao') else [])
    return {
        'Name': {
            'title': [{'type': 'text', 'text': {'content': f'{date} 每日归档'}}],
        },
        'Date': {'date': {'start': date}},
        'Type': {'select': {'name': '内容归档'}},
        'Category': {'multi_select': [{'name': '每日简报'}, {'name': '得到分析'}] if post.get('dedao') else [{'name': '每日简报'}]},
        'Source': {'rich_text': [{'type': 'text', 'text': {'content': 'daily-push / push-agent'}}]},
        'Status': {'select': {'name': '已归档'}},
        'Platform': {'multi_select': [{'name': 'Telegram'}, {'name': 'Web'}, {'name': 'Notion'}]},
        'Tags': {'multi_select': [{'name': tag} for tag in tags]},
        'Summary': {'rich_text': [{'type': 'text', 'text': {'content': summary[:2000]}}]},
    }


def clear_notion_page_children(api_key, page_id):
    cursor = None
    blocks = []
    while True:
        path = f'/blocks/{page_id}/children?page_size=100'
        if cursor:
            path += f'&start_cursor={cursor}'
        res = notion_request('GET', path, api_key)
        blocks.extend(res.get('results', []))
        if not res.get('has_more'):
            break
        cursor = res.get('next_cursor')
    for block in blocks:
        notion_request('DELETE', f"/blocks/{block['id']}", api_key)


def append_notion_children(api_key, page_id, children):
    for batch in children_batches(children):
        notion_request('PATCH', f'/blocks/{page_id}/children', api_key, {'children': batch})


def upsert_post_to_notion(post):
    api_key = (os.getenv('NOTION_API_KEY') or '').strip()
    if not api_key:
        print('ℹ️ Skipping Notion sync: NOTION_API_KEY not set')
        return False

    targets = resolve_notion_targets(api_key)
    if not targets:
        print('ℹ️ Skipping Notion sync: set NOTION_ARCHIVE_DATABASE_ID or NOTION_ARCHIVE_PARENT_PAGE_ID')
        return False

    database_id = targets['database_id']
    children = build_notion_children(post)
    properties = notion_page_properties(post)
    existing = notion_query_by_date(api_key, database_id, post['date'])

    if existing:
        page_id = existing[0]['id']
        notion_request('PATCH', f'/pages/{page_id}', api_key, {'properties': properties})
        clear_notion_page_children(api_key, page_id)
        append_notion_children(api_key, page_id, children)
        print(f"✅ Updated Notion archive for {post['date']}")
    else:
        page = notion_request('POST', '/pages', api_key, {
            'parent': {'database_id': database_id},
            'properties': properties,
            'children': [],
        })
        page_id = page['id']
        append_notion_children(api_key, page_id, children)
        print(f"✅ Created Notion archive for {post['date']}")

    return True


# ─── CLI ──────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description='Sync daily push content to web + Notion archive')
    parser.add_argument('--date', default=get_today(), help='Date to sync, format YYYY-MM-DD')
    parser.add_argument('--skip-notion', action='store_true', help='Skip Notion archive sync')
    parser.add_argument('--force', action='store_true', help='Sync even if posts/YYYY-MM-DD.json already exists')
    return parser.parse_args()


def main():
    args = parse_args()
    date = args.date
    post_file = os.path.join(POSTS_DIR, f'{date}.json')

    if os.path.exists(post_file) and not args.force:
        print(f'ℹ️ {date} already synced to web, loading existing post')
        with open(post_file, encoding='utf-8') as f:
            post = json.load(f)
    else:
        print(f'🔍 Looking for {date} briefing...')
        text = extract_briefing_text(date)
        if not text:
            print(f'❌ No briefing found for {date}')
            raise SystemExit(1)

        print(f'📝 Parsing briefing ({len(text)} chars)...')
        print('📚 Loading dedao analysis...')
        post = build_post(date, text)
        dedao_count = len(post['dedao'])
        print(f'   Found {dedao_count} dedao articles')
        save_and_push(post)
        print(f'🎉 Done! {date} is now on the website.')

    if not args.skip_notion:
        upsert_post_to_notion(post)


if __name__ == '__main__':
    main()

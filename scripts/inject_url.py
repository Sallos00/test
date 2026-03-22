import re, os

with open('auth.py', 'r', encoding='utf-8') as f:
    content = f.read()

url = os.environ.get('AUTH_SERVER_URL', '')
content = re.sub(
    r"SERVER_URL\s*=\s*os\.environ\.get\([^)]+\)",
    f'SERVER_URL = "{url}"',
    content
)

with open('auth.py', 'w', encoding='utf-8') as f:
    f.write(content)

print(f'SERVER_URL 삽입 완료: {url[:20]}...')

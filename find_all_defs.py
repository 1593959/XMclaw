with open(r'xmclaw\desktop\main_window.py', 'r', encoding='utf-8') as f:
    for i, line in enumerate(f, 1):
        if 'def _build_' in line or 'def _load_' in line or 'def _search_' in line:
            print(f'{i}: {line.rstrip()}')

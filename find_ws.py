with open(r'xmclaw\desktop\main_window.py', 'r', encoding='utf-8') as f:
    for i, line in enumerate(f, 1):
        if 'def _build_workspace' in line or 'def _build_evolution' in line:
            print(f'{i}: {line.rstrip()}')
        if 214 <= i <= 290:
            if i == 214 or i == 290:
                print(f'{i}: {line.rstrip()}')

with open(r'xmclaw\desktop\main_window.py', 'r', encoding='utf-8') as f:
    for i, line in enumerate(f, 1):
        if 170 <= i <= 220:
            print(f'{i}: {line.rstrip()}')

with open(r'xmclaw\desktop\main_window.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if 'build_workspace' in line or 'build_dashboard' in line or 'build_evolution' in line:
        print(f'{i+1}: {line.rstrip()}')

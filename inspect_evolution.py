with open(r'xmclaw\desktop\main_window.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
for i in range(213, 265):
    print(f"{i+1}: {lines[i].rstrip()}")

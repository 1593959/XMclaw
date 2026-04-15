with open(r'xmclaw\desktop\main_window.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
for i in range(160, 220):
    print(f"{i+1}: {lines[i].rstrip()}")

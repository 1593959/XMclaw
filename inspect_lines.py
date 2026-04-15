with open(r'C:\Users\15978\Desktop\XMclaw\xmclaw\desktop\main_window.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
for i, line in enumerate(lines[180:220], 181):
    print(i, repr(line))

with open(r'C:\Users\15978\Desktop\XMclaw\xmclaw\desktop\main_window.py', 'r', encoding='utf-8') as f:
    content = f.read()
print('Total lines:', len(content.splitlines()))
print('Last 50 lines:')
print('\n'.join(content.splitlines()[-50:]))

import re, sys, json
def sections(src):
    lines = src.split('\n')
    # <style> … </style> = CSS ; first <script> (classic) = engine ; <script type="module"> = module ; rest = HTML
    out = {'HTML':[], 'CSS':[], 'JS-engine':[], 'JS-module':[]}
    mode = 'HTML'
    for ln in lines:
        s = ln.strip()
        if mode == 'HTML':
            if s.startswith('<style'): mode = 'CSS'; out['HTML'].append(ln); continue
            if s.startswith('<script') and '</script' in s: out['HTML'].append(ln); continue   # 1 行で閉じる外部スクリプト
            if s.startswith('<script type="module"'): mode = 'JS-module'; out['HTML'].append(ln); continue
            if s.startswith('<script'): mode = 'JS-engine'; out['HTML'].append(ln); continue
            out['HTML'].append(ln)
        elif mode == 'CSS':
            if s.startswith('</style'): mode = 'HTML'; out['HTML'].append(ln); continue
            out['CSS'].append(ln)
        else:
            if s.startswith('</script'): mode = 'HTML'; out['HTML'].append(ln); continue
            out[mode].append(ln)
    return out
def classify(lines, kind):
    code = com = blank = trail = 0
    inblock = False
    for ln in lines:
        s = ln.strip()
        if not s: blank += 1; continue
        if kind == 'HTML':
            if inblock:
                com += 1
                if '-->' in s: inblock = False
                continue
            if s.startswith('<!--'):
                com += 1
                if '-->' not in s: inblock = True
                continue
            code += 1
            if '<!--' in s: trail += 1
        elif kind == 'CSS':
            if inblock:
                com += 1
                if '*/' in s: inblock = False
                continue
            if s.startswith('/*'):
                com += 1
                if '*/' not in s: inblock = True
                continue
            code += 1
            if '/*' in s: trail += 1
        else:
            if inblock:
                com += 1
                if '*/' in s: inblock = False
                continue
            if s.startswith('//'): com += 1; continue
            if s.startswith('/*'):
                com += 1
                if '*/' not in s: inblock = True
                continue
            code += 1
            if re.search(r"\s//\s", ln): trail += 1
    return dict(total=len(lines), code=code, comment=com, blank=blank, trail=trail)
def functions(js):
    # const name = (...) => { ... } / function name(  — 長さは次のトップレベル定義まで（粗い）
    names = []
    for i, ln in enumerate(js):
        m = re.match(r"^(?:const|let|function|async function)\s+([A-Za-z_$][\w$]*)\s*(?:=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>|\()", ln)
        if m: names.append((m.group(1), i))
    lens = []
    for k, (n, i) in enumerate(names):
        end = names[k+1][1] if k+1 < len(names) else len(js)
        # 関数の実体：終端の '}' 行まで（次の定義の直前の空行/コメントを除く）
        j = end - 1
        while j > i and (not js[j].strip() or js[j].strip().startswith('//')): j -= 1
        lens.append((j - i + 1, n))
    return len(names), sorted(lens, reverse=True)[:8]
def report(path):
    src = open(path, encoding='utf-8').read()
    sec = sections(src)
    r = {k: classify(v, 'HTML' if k=='HTML' else 'CSS' if k=='CSS' else 'JS') for k, v in sec.items()}
    tot = {kk: sum(r[k][kk] for k in r) for kk in ('total','code','comment','blank','trail')}
    js = sec['JS-module']; jt = '\n'.join(js); css = '\n'.join(sec['CSS'])
    nfun, longest = functions(js)
    lets = len(re.findall(r"^let\s", jt, re.M)) + sum(len(re.findall(r",\s*[A-Za-z_$][\w$]*\s*=", m)) for m in re.findall(r"^let\s[^\n]*", jt, re.M))
    lets_simple = sum(len(re.split(r",(?![^(]*\))", m)) for m in re.findall(r"^let\s+([^\n]*)", jt, re.M))
    ci = jt.find('const CALL_INIT'); cj = jt.find('\n}', ci)
    call_fields = len(re.findall(r"^\s+[A-Za-z_$][\w$]*\s*:", jt[ci:cj], re.M)) if ci >= 0 else 0
    d = dict(
        sections=r, total=tot, functions=nfun, longest=longest, module_lets=lets_simple, call_fields=call_fields,
        catch_empty=len(re.findall(r"catch\s*\{\s*\}", jt)), catch_arrow=len(re.findall(r"\.catch\(\(\)\s*=>\s*\{\s*\}\)", jt)),
        star_comments=len(re.findall(r"//.*★|/\*.*★|<!--.*★", src)), dated_comments=len(re.findall(r"20\d\d-\d\d-\d\d", src)),
        hidden_rules=len(re.findall(r"\[hidden\]\s*\{", css)), ls_direct=len(re.findall(r"localStorage\.(get|set|remove)Item", jt)),
        ls_set_untried=sum(1 for ln in js if 'localStorage.setItem' in ln and 'try' not in ln and 'lsSet' not in ln),
        style_display=len(re.findall(r"\.style\.display\s*=", jt)), send_send=len(re.findall(r"\.send\(", jt)), sendTo=len(re.findall(r"\bsendTo\(", jt)),
        tooSoon=len(re.findall(r"\btooSoon\(", jt)), seen_maps=len(re.findall(r"Seen\.set\(", jt)),
        confirm_alert=len(re.findall(r"\b(confirm|alert|prompt)\(", jt)),
        runJoin=next((l for l, n in longest if n == 'runJoin'), None),
    )
    return d
for p in sys.argv[1:]:
    d = report(p)
    print('==', p)
    for k in ('HTML','CSS','JS-engine','JS-module'): print('  %-10s'%k, d['sections'][k])
    print('  total', d['total'])
    for k in ('functions','longest','module_lets','call_fields','catch_empty','catch_arrow','star_comments','dated_comments','hidden_rules','ls_direct','ls_set_untried','style_display','send_send','sendTo','tooSoon','seen_maps','confirm_alert'): print('  %s=%s' % (k, d[k]))

import urllib.request

with urllib.request.urlopen("http://127.0.0.1:5000/") as r:
    html = r.read().decode("utf-8")

checks = [
    ("repeat-toggle button",        'id="repeat-toggle"' in html),
    ("Repeat: Off label",           "Repeat: Off" in html),
    ("theme-toggle button",         'id="theme-toggle"' in html),
    ("icon-sun SVG",                'class="icon-sun"' in html),
    ("icon-moon SVG",               'class="icon-moon"' in html),
    ("light theme CSS rule",        ':root[data-theme="light"]' in html),
    ("CSS var --bg defined",        "--bg:" in html),
    ("audio uses --audio-filter",   "filter: var(--audio-filter)" in html),
    ("early-theme script present",  "maniac-theme" in html),
    ("Shuffle still present",       'id="shuffle-toggle"' in html),
    ("Stop button still present",   'id="btn-stop"' in html),
    ("Next button still present",   'id="btn-next"' in html),
]
print(f"HTML size: {len(html)} bytes\n")
for name, ok in checks:
    tag = "OK  " if ok else "FAIL"
    print(f"  [{tag}] {name}")
print()
print("ALL CHECKS PASSED" if all(ok for _, ok in checks) else "SOME CHECKS FAILED")

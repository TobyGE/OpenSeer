---
name: web-and-news
description: Pull news, search results, or page content via bash (curl + RSS / urllib + lightweight parsing) — much faster and more reliable than driving a browser through CU.
family: bash
requires:
  bins: ['curl', 'python3']
---

# Web search, news, page fetch via bash

For "today's news / market update / search results / fetch URL X",
bash + curl + python3 is ~10x faster and more reliable than driving
Safari via CU. Use CU only if the task needs interaction (login, form,
click-through) or the page is JS-rendered.

## RSS / Atom — structured news in one shot

```bash
python3 - <<'PY'
import urllib.request, xml.etree.ElementTree as ET
URL = "https://feeds.bbci.co.uk/news/rss.xml"
req = urllib.request.Request(URL, headers={"User-Agent": "openseer/0.1"})
data = urllib.request.urlopen(req, timeout=10).read()
for item in ET.fromstring(data).iter("item"):
    title = (item.findtext("title") or "").strip()
    link  = (item.findtext("link") or "").strip()
    print(f"- {title}\n  {link}")
PY
```

Useful feeds (try 2 and merge — no single feed covers everything):
- General: `https://feeds.bbci.co.uk/news/rss.xml`,
  `https://hnrss.org/frontpage`
- 中文财经: `https://feedx.net/rss/sina_money.xml`,
  `https://rsshub.app/eastmoney/news/zhuzhang`

## Plain page fetch — strip HTML to text

```bash
curl -sL --max-time 10 -A 'openseer/0.1' '<URL>' | \
python3 -c 'import sys,re; h=sys.stdin.read();
h=re.sub(r"<(script|style).*?</\1>","",h,flags=re.S|re.I);
h=re.sub(r"<[^>]+>"," ",h); print(re.sub(r"\s+"," ",h).strip()[:4000])'
```

## Free fact APIs (no key)

```bash
# Wikipedia article summary
curl -s "https://en.wikipedia.org/api/rest_v1/page/summary/Geoffrey_Hinton" | \
  python3 -c "import sys,json; print(json.load(sys.stdin).get('extract'))"
```

For Google-quality web search you need a paid API key (Tavily / Serper
/ Brave). Direct search-engine HTML scraping is unreliable.

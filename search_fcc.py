from curl_cffi import requests
from bs4 import BeautifulSoup
resp = requests.get('https://html.duckduckgo.com/html/', params={'q': '"AR-8000" site:fccid.io'}, impersonate="chrome124", headers={'User-Agent': 'Mozilla/5.0'})
soup = BeautifulSoup(resp.text, 'html.parser')
for a in soup.find_all('a', class_='result__snippet'):
    print(a.text)

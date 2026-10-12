import urllib.request
import sys
import os

url = "https://drive.google.com/uc?export=download&id=1TqleXec_OykOYFREKKtschzY29dUcVAQ&confirm=t"
target = "spider.zip"

print(f"Downloading spider dataset from {url}...")
try:
    urllib.request.urlretrieve(url, target)
    size = os.path.getsize(target)
    print(f"Downloaded: {size} bytes")
    if size < 50000:
        with open(target, 'r', encoding='utf-8', errors='ignore') as f:
            print("Preview:", f.read()[:500])
except Exception as e:
    print("Download error:", e)

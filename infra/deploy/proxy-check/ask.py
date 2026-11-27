import sys, urllib.request
print(urllib.request.urlopen(sys.argv[1], timeout=15).read().decode())

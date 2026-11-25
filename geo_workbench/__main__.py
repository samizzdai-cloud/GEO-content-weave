"""Cross-platform local launcher: python -m geo_workbench --open."""
import argparse
import threading
import time
import urllib.error
import urllib.request
import webbrowser


def open_when_ready(url,timeout=30):
    # Do not send loopback readiness checks through an external HTTP proxy.
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            with opener.open(url,timeout=1) as response:
                if response.status==200:
                    webbrowser.open(url)
                    return True
        except (OSError,urllib.error.URLError):pass
        time.sleep(0.25)
    return False


def main(argv=None):
    parser=argparse.ArgumentParser(description='启动本地 GEO 内容工作台')
    parser.add_argument('--port',type=int,default=8765,help='本地端口（默认 8765）')
    parser.add_argument('--open',action='store_true',help='服务就绪后打开默认浏览器')
    args=parser.parse_args(argv)
    if not 1<=args.port<=65535:parser.error('端口须在 1–65535 之间')
    if args.open:
        threading.Thread(target=open_when_ready,args=(f'http://127.0.0.1:{args.port}/',),daemon=True).start()
    import uvicorn
    uvicorn.run('geo_workbench.app:app',host='127.0.0.1',port=args.port,workers=1)


if __name__=='__main__':main()

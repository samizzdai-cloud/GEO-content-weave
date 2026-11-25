"""Read-only model-list connectivity probe. No generation and no key output."""
import os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import geo_workbench  # Loads local .env before adapters are configured.
import httpx


def main():
    ark=next((v for k,v in os.environ.items() if k.casefold() in ('seedream_api_key','ark_api_key') and v),None)
    endpoints=[('DeepSeek','https://api.deepseek.com/models',os.getenv('DEEPSEEK_API_KEY')),
               ('Seedream / Ark','https://ark.cn-beijing.volces.com/api/v3/models',ark)]
    failed=False
    with httpx.Client(timeout=20,trust_env=True) as client:
        for name,url,key in endpoints:
            if not key:print(name+': 未配置凭证，跳过');continue
            try:
                response=client.get(url,headers={'Authorization':'Bearer '+key})
                print(name+': HTTP '+str(response.status_code))
                failed=failed or response.status_code!=200
            except httpx.HTTPError as exc:
                print(name+': 连接失败（'+type(exc).__name__+'），请检查代理／DNS／证书')
                failed=True
    return int(failed)


if __name__=='__main__':raise SystemExit(main())

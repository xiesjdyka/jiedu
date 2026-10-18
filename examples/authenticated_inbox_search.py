"""Search approved inbox contents through authenticated localhost HTTP.
Set JIEDU_CLIENT_ID and JIEDU_CLIENT_SECRET from the client's pairing result.
Does not alter permissions, initialize a workspace or read credential files.
"""
import argparse,json,os,sys,urllib.request,urllib.error

def request(port,query):
    client=os.environ.get('JIEDU_CLIENT_ID','');secret=os.environ.get('JIEDU_CLIENT_SECRET','')
    if not client or not secret:raise ValueError('Set JIEDU_CLIENT_ID and JIEDU_CLIENT_SECRET from your paired client.')
    if any(c in client+secret for c in '\r\n') or ':' in client:raise ValueError('Invalid credential format.')
    if not 1<=port<=65535:raise ValueError('Invalid port.')
    body=json.dumps({'scope':'inbox','query':query,'search_mode':'keyword','limit':5},ensure_ascii=False).encode('utf-8')
    return urllib.request.Request(f'http://127.0.0.1:{port}/api/client-access/knowledge/search',data=body,
        headers={'Content-Type':'application/json','Authorization':f'JieduClient {client}:{secret}'},method='POST')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('query');parser.add_argument('--port',type=int,default=8765);args=parser.parse_args()
    try:
        req=request(args.port,args.query)
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=30) as result:
            print(json.dumps(json.load(result),ensure_ascii=False,indent=2))
        return 0
    except urllib.error.HTTPError as exc:
        print(f'Request refused (HTTP {exc.code}). Check pairing, inbox search permission and service state.',file=sys.stderr);return 1
    except (ValueError,urllib.error.URLError):
        print('Request failed. Check credentials, port and local service; no secret values are logged.',file=sys.stderr);return 1

if __name__=='__main__':raise SystemExit(main())

import os,time
import jwt,requests
# Same application and installation as ci_v7x/startup-script.sh.tftpl.
key=os.environ['_BK_TEMP_GITHUB_APP_PEM']
signed=jwt.encode({'iat':int(time.time())-30,'exp':int(time.time())+600,'iss':'4156238'},key,algorithm='RS256')
repo=os.environ.get('BUILDKITE_REPO','')
name=repo.rstrip('/').split('/')[-1].removesuffix('.git')
if not name:
    raise SystemExit('Repository-scoped GitHub credential requires BUILDKITE_REPO')
r=requests.post('https://api.github.com/app/installations/142868369/access_tokens',headers={'Authorization':'Bearer '+signed,'Accept':'application/vnd.github+json'},json={'repositories':[name],'permissions':{'contents':'read'}},timeout=30)
r.raise_for_status()
print('username=x-access-token')
print('password='+r.json()['token'])

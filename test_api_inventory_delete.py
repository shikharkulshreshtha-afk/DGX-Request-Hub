import json, urllib.request, urllib.error
BASE='http://127.0.0.1:8000'

def request(path, method='GET', token=None, data=None):
    req = urllib.request.Request(BASE+path, method=method)
    req.add_header('Content-Type','application/json')
    if token: req.add_header('Authorization','Bearer '+token)
    if data is not None: req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode()
            return r.status, json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

# login
s, b = request('/api/auth/login', 'POST', None, {'email':'admin@dgx.local','password':'admin1234'})
if s!=200:
    print('Login failed', s, b); raise SystemExit(1)
token = b['token']
print('Logged in, token len', len(token))

# create item
s, b = request('/api/admin/inventory', 'POST', token, {'resource_type':'FULL_GPU','label':'TEST-GPU-DELETE','status':'AVAILABLE'})
print('Create', s)
if isinstance(b, dict):
    item_id = b.get('id')
    print('Created id', item_id)
else:
    print('Create response', b); raise SystemExit(1)

# delete item
s, b = request(f'/api/admin/inventory/{item_id}', 'DELETE', token)
print('Delete', s, b)

# re-list
s, b = request('/api/admin/inventory', 'GET', token)
if s==200:
    ids=[i.get('id') for i in b.get('inventory',[])]
    print('Contains deleted?', item_id in ids)
    matches=[i for i in b.get('inventory',[]) if i.get('label','').startswith('TEST-GPU-DELETE')]
    print('Matches', matches)
else:
    print('List err', s, b)

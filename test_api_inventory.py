import json
import urllib.request
import urllib.parse

BASE = 'http://127.0.0.1:8000'

def api(path, method='GET', token=None, data=None):
    url = BASE + path
    req = urllib.request.Request(url, method=method)
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    if data is not None:
        body = json.dumps(data).encode('utf-8')
        req.data = body
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            print('STATUS', res.status)
            print(res.read().decode())
    except urllib.error.HTTPError as e:
        print('HTTP', e.code)
        print(e.read().decode())
    except Exception as e:
        print('ERR', e)

# login admin
print('Logging in...')
req = urllib.request.Request(BASE + '/api/auth/login', method='POST')
req.add_header('Content-Type', 'application/json')
creds = json.dumps({'email': 'admin@dgx.local', 'password': 'admin1234'}).encode('utf-8')
req.data = creds
with urllib.request.urlopen(req) as res:
    body = json.loads(res.read().decode())
    token = body['token']
    print('Token length', len(token))

print('\nGET /api/inventory')
api('/api/inventory', 'GET', token)

print('\nPOST /api/admin/inventory')
api('/api/admin/inventory', 'POST', token, {'resource_type': 'FULL_GPU', 'label': 'TEST-GPU-1', 'status': 'AVAILABLE'})

print('\nGET /api/inventory after create')
api('/api/inventory', 'GET', token)

# try PATCH on created item: find the id from previous call manually if needed
print('\nDone')

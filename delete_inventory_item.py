#!/usr/bin/env python3
import sys, json, urllib.request, urllib.error

if len(sys.argv) < 2:
    print('Usage: delete_inventory_item.py <ITEM_ID>')
    sys.exit(2)

ITEM_ID = sys.argv[1]
BASE = 'http://127.0.0.1:8000'
ADMIN_EMAIL = 'admin@dgx.local'
ADMIN_PASS = 'admin1234'

def login():
    req = urllib.request.Request(BASE + '/api/auth/login', method='POST')
    req.add_header('Content-Type','application/json')
    req.data = json.dumps({'email': ADMIN_EMAIL, 'password': ADMIN_PASS}).encode()
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode())
            return resp.get('token')
    except urllib.error.HTTPError as e:
        print('Login failed:', e.code, e.read().decode())
        return None


def delete_item(token, item_id):
    url = f"{BASE}/api/admin/inventory/{item_id}"
    req = urllib.request.Request(url, method='DELETE')
    req.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print('DELETE status', r.status)
            print(r.read().decode())
    except urllib.error.HTTPError as e:
        print('DELETE failed:', e.code, e.read().decode())


def list_inventory(token):
    url = BASE + '/api/admin/inventory'
    req = urllib.request.Request(url, method='GET')
    req.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
            return data
    except urllib.error.HTTPError as e:
        print('List failed:', e.code, e.read().decode())
        return None


if __name__ == '__main__':
    token = login()
    if not token:
        sys.exit(1)
    print('Token acquired, length', len(token))
    print('\nDeleting item', ITEM_ID)
    delete_item(token, ITEM_ID)
    print('\nVerifying inventory...')
    data = list_inventory(token)
    if not data:
        sys.exit(1)
    ids = [i.get('id') for i in data.get('inventory', [])]
    if ITEM_ID in ids:
        print('\nResult: ITEM STILL PRESENT')
    else:
        print('\nResult: ITEM REMOVED')

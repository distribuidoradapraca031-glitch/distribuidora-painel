#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cliente do GestãoClick para o app web: leitura e gravação, com rate limit e retry.
Reaproveita os tokens do .env da distribuidora."""
import json, os, time, urllib.request, urllib.parse, urllib.error

BASE = "https://api.gestaoclick.com"
ENV_PATH = os.environ.get("GC_ENV_PATH", "/Users/lucchesi/Pessoal/distribuidora/.env")

def _load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env

_ENV = _load_env(ENV_PATH)

def _tok(name):
    return os.environ.get(name) or _ENV.get(name) or ""

HEADERS = {
    "access-token": _tok("CRM_ACCESS_TOKEN"),
    "secret-access-token": _tok("CRM_SECRET_ACCESS_TOKEN"),
    "Content-Type": "application/json",
}

_last_call = [0.0]
_MIN_GAP = 0.35  # ~< 3 req/s

def _throttle():
    dt = time.time() - _last_call[0]
    if dt < _MIN_GAP:
        time.sleep(_MIN_GAP - dt)
    _last_call[0] = time.time()

def _req(path, method="GET", params=None, body=None, max_retries=5):
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{BASE}{path}{qs}"
    data = json.dumps(body).encode() if body is not None else None
    last_err = None
    for attempt in range(max_retries):
        _throttle()
        req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(1.5 * (attempt + 1)); continue
            corpo = e.read()[:300].decode("utf-8", "ignore")
            last_err = f"HTTP {e.code}: {corpo}"
            # A API do GestãoClick cai sozinha de vez em quando e devolve 404
            # "Controller class VendasController could not be found" ou 5xx — some e volta
            # em segundos. Em LEITURA vale insistir (foi o que derrubou o fechamento de
            # caixa em 01/09/2026, aparecendo como "erro de conexão" pro dono). Em POST/PUT
            # não: o lançamento pode ter sido gravado e repetir duplicaria.
            transitorio = e.code >= 500 or (e.code == 404 and "could not be found" in corpo)
            if transitorio and method == "GET" and attempt < max_retries - 1:
                time.sleep(1.0 * (attempt + 1)); continue
            raise RuntimeError(last_err)
        except Exception as e:
            last_err = str(e); time.sleep(1.0)
    raise RuntimeError(last_err or f"falhou: {url}")

def get(path, params=None):
    return _req(path, "GET", params=params)

def post(path, body):
    return _req(path, "POST", body=body)

def put(path, body):
    return _req(path, "PUT", body=body)

def delete(path):
    return _req(path, "DELETE")

def get_all(path, params=None, hard_cap=5000):
    """Puxa todas as páginas (usa meta.proxima_pagina). Achata o wrapper de recurso."""
    out = []
    page = 1
    params = dict(params or {})
    while len(out) < hard_cap:
        params["pagina"] = page
        params["limite"] = 100
        d = get(path, params)
        rows = d.get("data") or []
        for row in rows:
            # cada item vem como {"Produto": {...}} ou já achatado
            if isinstance(row, dict) and len(row) == 1:
                out.append(next(iter(row.values())))
            else:
                out.append(row)
        if not (d.get("meta") or {}).get("proxima_pagina"):
            break
        page += 1
    return out

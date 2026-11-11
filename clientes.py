#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Clientes do delivery: quem pede, quantas vezes, quando foi a última (pedido do dono, 11/11/2026).

O Anota AI só guarda o detalhe do pedido (nome e telefone do cliente) por uns 7 dias: em 11/11
o pedido de 03/11 já voltava 410. Então o histórico é montado DAQUI PRA FRENTE: a cada volta do
motor, os pedidos do app que entraram no GestãoGPT nos últimos dias são lidos no Anota e somados
na ficha do cliente.

Onde mora: produtos técnicos inativos "CONTROLE CLIENTES n" no GestãoGPT (o mesmo truque do
CONTROLE PAINEL: um JSON cortado em pedaços de 2.400 letras). NUNCA no painel_data.json, que vai
pro repositório: nome e telefone de cliente não podem ficar públicos.

Formato: {"c": {id_cliente: [nome, fone, pedidos, total, primeira, ultima]},
          "r": {nº do pedido: [dia, total, id_cliente]}}   (r guarda só os últimos 30 dias)
"""
import json, re, threading, time, urllib.parse
from datetime import datetime, timedelta, timezone

import gg_compat as gg

BRT = timezone(timedelta(hours=-3))
NOME = "CONTROLE CLIENTES"
MAX = 2400            # o gatilho de auditoria do GestãoGPT estoura com texto grande (ver gg_compat)
DIAS_OLHA = 6         # o Anota apaga o detalhe com ~7 dias
DIAS_R = 30
_lock = threading.Lock()
_cache = {"em": 0.0, "dado": None, "ids": None, "txt": {}}


def _hoje():
    return datetime.now(BRT).date()


def _ids(fresco=False):
    if _cache["ids"] is None or fresco:
        q = urllib.parse.quote(NOME)
        _cache["ids"] = sorted(p["ID"] for p in gg._todas(f"/produto?inativo=true&descricao={q}")
                               if (p.get("DESCRICAO") or "").startswith(NOME))
    return _cache["ids"]


def ler(fresco=False):
    """Ficha de todos os clientes. Cache de 5 min (o motor grava e já deixa a cópia nova)."""
    if not fresco and _cache["dado"] is not None and time.time() - _cache["em"] < 300:
        return _cache["dado"]
    ids = _ids(fresco=True)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(6) as ex:
        txts = list(ex.map(lambda pid: gg._req("GET", f"/produto/{pid}").get("INFOADICIONAL") or "", ids))
    for pid, t in zip(ids, txts):
        _cache["txt"][pid] = t
    txt = "".join(txts).strip()
    try:
        dado = json.loads(txt) if txt else {}
    except ValueError:
        raise RuntimeError("a lista de clientes veio quebrada do GestãoGPT; tente de novo em 1 minuto")
    dado.setdefault("c", {})
    dado.setdefault("r", {})
    _cache["dado"], _cache["em"] = dado, time.time()
    return dado


def _grava(dado):
    # só ASCII: o banco deles troca alguns caracteres (o travessão volta como \x14)
    txt = json.dumps(dado, ensure_ascii=True, separators=(",", ":"))
    pedacos = [txt[i:i + MAX] for i in range(0, len(txt), MAX)]
    ids = _ids(fresco=True)
    while len(ids) < len(pedacos):
        k = len(ids) + 1
        r = gg._req("POST", "/produto", {"descricao": f"{NOME} {k}", "codfiscal": f"CTRL-CLIENTES-{k}",
                                         "tipo": "SERVIÇO", "unidmed": "UN", "inativo": True, "infoadicional": ""})
        ids.append(r["ID"])
    for i, pid in enumerate(ids):
        t = pedacos[i] if i < len(pedacos) else ""
        if _cache["txt"].get(pid) != t:
            gg._req("PUT", f"/produto/{pid}", {"infoadicional": t})
            _cache["txt"][pid] = t
    _cache["dado"], _cache["em"] = json.loads(txt), time.time()


def _fone(s):
    d = re.sub(r"\D", "", s or "")
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    return d


def atualiza(anota_mod, gravar=True):
    """Lê no Anota os pedidos do app dos últimos dias que ainda não estão na ficha e soma.
    Devolve quantos entraram. Idempotente: o nº do pedido fica marcado em "r"."""
    if not _lock.acquire(blocking=False):
        return {"novos": 0, "ocupado": True}
    try:
        dado = ler(fresco=True)
        c, r = dado["c"], dado["r"]
        ini = (_hoje() - timedelta(days=DIAS_OLHA)).isoformat()
        contas = [k for k in gg._todas(f"/conta?tipo=receber&status=todos&data_venc_inicio={ini}")
                  if (k.get("REFBUSCA") or "").startswith("ANOTA-")]
        faltam = [k for k in contas if k["REFBUSCA"][6:] not in r]
        novos, sem_cliente, mudou = 0, 0, False
        if faltam:
            ah = anota_mod._headers()
            for k in faltam:
                ref = k["REFBUSCA"][6:]
                dia, total, cid = (k.get("DATA_EMIS") or "")[:10], float(k.get("VALOR") or 0), ""
                try:
                    obs = gg._req("GET", f"/pedido/{k['ID_PEDIDO']}").get("OBS") or ""
                    m = re.search(r"\(([0-9a-f]{24})\)", obs)
                    det = {}
                    if m:
                        det = anota_mod._http("GET", anota_mod.GW + f"/api-old/partnerauth/v2/ping/get/{m.group(1)}",
                                              headers=ah).get("info") or {}
                except Exception:
                    continue                       # tenta de novo na próxima volta
                cli = det.get("customer") or {}
                if det.get("createdAt"):
                    dia = (datetime.fromisoformat(det["createdAt"].replace("Z", "+00:00"))
                           .astimezone(BRT).strftime("%Y-%m-%d"))
                    total = float(det.get("total") or total)
                fone = _fone(cli.get("phone"))
                cid = str(cli.get("id") or fone or "")
                if cid:
                    nome = " ".join((cli.get("name") or "").split())[:40]
                    f = c.get(cid) or [nome, fone, 0, 0.0, dia, dia]
                    f[0], f[1] = nome or f[0], fone or f[1]
                    f[2] += 1
                    f[3] = round(f[3] + total, 2)
                    f[4], f[5] = min(f[4], dia), max(f[5], dia)
                    c[cid] = f
                    novos += 1
                else:
                    sem_cliente += 1               # o Anota já apagou: conta o pedido, sem ficha
                r[ref] = [dia, round(total, 2), cid]
                mudou = True
        corte = (_hoje() - timedelta(days=DIAS_R)).isoformat()
        for ref in [x for x, v in r.items() if (v[0] if isinstance(v, list) else v) < corte]:
            r.pop(ref)
            mudou = True
        if mudou and gravar:
            _grava(dado)
        return {"novos": novos, "sem_cliente": sem_cliente, "olhados": len(faltam), "clientes": len(c)}
    finally:
        _lock.release()


def resumo(semana_ini=None):
    """Listas pro painel: quem sumiu, os melhores e os novos da semana."""
    dado = ler()
    hoje = _hoje()
    hj = hoje.isoformat()
    seg = semana_ini or (hoje - timedelta(days=hoje.weekday())).isoformat()
    fichas = []
    for cid, (nome, fone, n, total, pri, ult) in dado["c"].items():
        dias_sem = (hoje - datetime.fromisoformat(ult).date()).days
        intervalo = None
        if n > 1:
            intervalo = round((datetime.fromisoformat(ult) - datetime.fromisoformat(pri)).days / (n - 1), 1)
        fichas.append({"id": cid, "nome": nome or "Cliente sem nome", "fone": fone, "pedidos": n,
                       "total": total, "primeira": pri, "ultima": ult, "dias_sem": dias_sem,
                       "intervalo": intervalo})
    # sumiu: pedia de costume (2 ou mais pedidos) e está sem pedir há 3 semanas ou o dobro do ritmo dele
    sumiram = [f for f in fichas if f["pedidos"] >= 2 and f["dias_sem"] >= max(21, 2 * (f["intervalo"] or 0))]
    sumiram.sort(key=lambda f: -f["total"])
    melhores = sorted(fichas, key=lambda f: (-f["total"], -f["pedidos"]))[:15]
    novos = sorted([f for f in fichas if f["primeira"] >= seg], key=lambda f: f["primeira"], reverse=True)
    voltaram = [f for f in fichas if f["primeira"] < seg <= f["ultima"]]
    desde = min([f["primeira"] for f in fichas] or [hj])
    return {"hoje": hj, "semana_ini": seg, "desde": desde, "total_clientes": len(fichas),
            "sumiram": sumiram[:30], "melhores": melhores, "novos": novos[:30],
            "n_novos": len(novos), "n_voltaram": len(voltaram),
            "uma_vez": sum(1 for f in fichas if f["pedidos"] == 1)}

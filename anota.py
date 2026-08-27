#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Robô do delivery: puxa os pedidos do app (Anota AI) e lança no GestãoClick com NFC-e.

É a mesma rotina que rodava no MacBook do dono (scripts/sync_anota_gc.py), agora dentro
do app web pra não depender de computador ligado — quando o Mac dormia, o robô parava e
o pedido sumia da lista do Anota antes de ser importado (26/08/2026: 3 pedidos ficaram
de fora, e outros dias têm buraco maior).

O controle do que já entrou NÃO fica em arquivo: sai do próprio GestãoClick, lendo o
número do pedido na observação da venda. Assim o disco efêmero da nuvem não duplica nada
e a rotina do Mac pode continuar rodando junto sem lançar o mesmo pedido duas vezes.
"""
import json, os, re, time, unicodedata, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone

import gclient as gcapi

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAP_PATH = os.path.join(BASE_DIR, "anota_gc_map.json")
GW = "https://gateway-partners.anota.ai"
UA = "DistribuidoraDaPraca-Integracao/1.0"
BRT = timezone(timedelta(hours=-3))

CHECK_OK = {2, 3}                     # pronto / finalizado
GC_CLIENTE_DELIVERY = "55346041"      # cliente "DELIVERY"
GC_SITUACAO_CONCRETIZADA = "8468151"  # mesma situação do PDV
GC_LOJA = "529233"
GC_CFOP_VENDA_BALCAO = "289"          # CFOP 5102, igual ao PDV
GC_FORMAS = {"pix": "6055931", "dinheiro": "6055919",
             "credito": "6055920", "debito": "6055921"}
DIAS_JANELA = 4                       # quantos dias de venda o dedupe olha no GC


def _env(nome):
    return os.environ.get(nome) or gcapi._tok(nome)


def _http(method, url, body=None, headers=None, form=False):
    h = {"User-Agent": UA}
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"http": e.code, "body": e.read().decode("utf-8", "ignore")[:400]}


def _headers():
    r = _http("POST", GW + "/integ/integ-oauth-api/oauth-client/token", {
        "grant_type": "client_credentials",
        "client_id": _env("ANOTA_CLIENT_ID"),
        "client_secret": _env("ANOTA_CLIENT_SECRET"),
    }, form=True)
    tok = r.get("accessToken")
    if not tok:
        raise RuntimeError(f"Anota AI não devolveu token: {json.dumps(r)[:200]}")
    return {"Authorization": "Bearer " + tok, "x-page-id": _env("ANOTA_PAGE_ID")}


def norm(s):
    s = unicodedata.normalize("NFD", (s or "").upper())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^A-Z0-9,.]", " ", s)
    s = re.sub(r"(\d+[.,]?\d*)\s*(ML|MLS)\b", lambda m: m.group(1).replace(",", ".") + "ML", s)
    s = re.sub(r"(\d+[.,]?\d*)\s*(LTS|LT|LS|L)\b", lambda m: m.group(1).replace(",", ".") + "L", s)
    s = re.sub(r"[,.](?!\d)", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_mapa_cache = [None]

def mapa():
    if _mapa_cache[0] is None:
        with open(MAP_PATH, encoding="utf-8") as f:
            _mapa_cache[0] = json.load(f)
    return _mapa_cache[0]


def payment_forma(p):
    txt = ((p.get("code") or "") + " " + (p.get("name") or "")).lower()
    if "pix" in txt:
        return GC_FORMAS["pix"], "PIX"
    if "credit" in txt or "crédito" in txt or "credito" in txt:
        return GC_FORMAS["credito"], "Cartão de Crédito"
    if "debit" in txt or "débito" in txt or "debito" in txt:
        return GC_FORMAS["debito"], "Cartão de Débito"
    if "card" in txt or "cartao" in txt or "cartão" in txt:
        return GC_FORMAS["credito"], "Cartão de Crédito"   # Anota manda só "card" (decisão do dono)
    if "money" in txt or "cash" in txt or "dinheiro" in txt:
        return GC_FORMAS["dinheiro"], "Dinheiro à Vista"
    if p.get("prepaid"):
        return GC_FORMAS["pix"], "PIX"
    return None, None


def _hoje_brt():
    return datetime.now(BRT).strftime("%Y-%m-%d")


def ja_importados():
    """Nºs de pedido do app que já viraram venda no GestãoClick (janela curta).

    É o dedupe: a observação da venda guarda 'pedido #1234 (id do Anota)'.
    """
    hoje = datetime.now(BRT).date()
    ini = (hoje - timedelta(days=DIAS_JANELA)).isoformat()
    refs, oids = set(), set()
    for v in gcapi.get_all("/vendas", {"tipo": "produto", "data_inicio": ini,
                                       "data_fim": hoje.isoformat()}):
        o = v.get("observacoes") or ""
        if "ANOTA AI" not in o.upper():
            continue
        m = re.search(r"#(\d+)", o)
        if m:
            refs.add(m.group(1))
        m2 = re.search(r"\(([0-9a-f]{16,})\)", o)
        if m2:
            oids.add(m2.group(1))
    return refs, oids


def emit_nfce(venda_id, data_brt, forma_pagamento_id):
    """Emite a NFC-e da venda espelhando o PDV: presencial, sem CPF, CFOP 5102.
    A taxa de entrega fica fora da nota (a API do GC ignora o campo de frete)."""
    try:
        v = gcapi.get(f"/vendas/{venda_id}").get("data") or {}
        # unitário já arredondado em 2 casas — é assim que o GC grava na nota. Mandando o
        # valor cheio, o total do pagamento nascia diferente do total da nota e a correção
        # por PUT reprovava a nota na SEFAZ ("Rejeição 899: meio de pagamento incorreto").
        prods = [{"produto_id": p["produto"]["produto_id"],
                  "quantidade": float(p["produto"]["quantidade"]),
                  "valor_venda": round(float(p["produto"]["valor_venda"]), 2)}
                 for p in v.get("produtos", [])]
        if not prods:
            return None
        dt = datetime.strptime(data_brt, "%Y-%m-%d").strftime("%d/%m/%Y")
        body = {
            "loja_id": GC_LOJA, "pedido_id": str(venda_id), "tipo_atendimento": 1,
            "tipo_nf": "1", "consumidor_final": 1, "natureza_operacao": "Venda balcão",
            "cfop_id": GC_CFOP_VENDA_BALCAO, "produtos": prods,
            "pagamento": [{"forma_pagamento_id": forma_pagamento_id,
                           "valor_pagamento": round(sum(p["quantidade"] * p["valor_venda"]
                                                        for p in prods), 2),
                           "data_vencimento": dt}],
        }
        r = gcapi.post("/notas_fiscais_consumidores", body)
        nid = (r.get("data") or {}).get("dados")
        if not nid:
            return None
        n = gcapi.get(f"/notas_fiscais_consumidores/{nid}").get("data") or {}
        tot = round(sum(float(p["valor_venda"]) for p in n.get("produtos", [])), 2)
        if abs(tot - body["pagamento"][0]["valor_pagamento"]) > 0.001:
            # nunca consertar com PUT (reprova na SEFAZ): apaga o rascunho e refaz
            try:
                gcapi.delete(f"/notas_fiscais_consumidores/{nid}")
            except Exception:
                pass
            body["pagamento"][0]["valor_pagamento"] = tot
            r = gcapi.post("/notas_fiscais_consumidores", body)
            nid = (r.get("data") or {}).get("dados")
            if not nid:
                return None
        e = gcapi.post(f"/notas_fiscais_consumidores/emitir/{nid}", {})
        if (e.get("data") or {}).get("ok"):
            n = gcapi.get(f"/notas_fiscais_consumidores/{nid}").get("data") or {}
            return n.get("numero_nf") or nid
        return None
    except Exception:
        return None


def rodar(post=True, dias=3):
    """Puxa a lista de pedidos do Anota AI e lança no GestãoClick o que ainda não entrou.
    Devolve um resumo pro painel (não levanta exceção pro chamador)."""
    ini = time.time()
    res = {"quando": datetime.now(BRT).strftime("%d/%m/%Y %H:%M"), "novos": [],
           "pendentes": [], "erros": [], "na_lista": 0, "ok": True}
    try:
        ah = _headers()
        docs, page = [], 1
        while True:
            r = _http("GET", GW + f"/api-old/partnerauth/v2/ping/list?currentpage={page}", headers=ah)
            info = r.get("info") or {}
            docs += info.get("docs") or []
            if len(docs) >= (info.get("count") or 0) or not info.get("docs"):
                break
            page += 1
        res["na_lista"] = len(docs)
        if not docs:
            res["segundos"] = round(time.time() - ini, 1)
            return res

        refs, oids = ja_importados()
        pmap = mapa()
        limite = (datetime.now(BRT).date() - timedelta(days=dias)).isoformat()

        for d in docs:
            oid = d.get("_id")
            if not oid or oid in oids:
                continue
            det = _http("GET", GW + f"/api-old/partnerauth/v2/ping/get/{oid}", headers=ah).get("info") or {}
            ref = str(det.get("shortReference") or "")
            if ref and ref in refs:
                continue
            created = det.get("createdAt") or ""
            data_brt = ""
            if created:
                data_brt = (datetime.fromisoformat(created.replace("Z", "+00:00"))
                            .astimezone(BRT).strftime("%Y-%m-%d"))
            if d.get("check") not in CHECK_OK:
                continue                       # ainda não saiu pra entrega
            if data_brt and data_brt < limite:
                continue                       # pedido velho: não reabre histórico

            produtos, faltando, soma = [], [], 0.0
            for it in det.get("items", []):
                qty = float(it.get("quantity") or 1)
                price = float(it.get("price") or 0)
                for sub in it.get("subItems") or []:
                    price += float(sub.get("price") or 0) * float(sub.get("quantity") or 1)
                soma += qty * price
                m = pmap.get(norm(it.get("name") or ""))
                if not m or not m.get("gc_id"):
                    faltando.append(it.get("name"))
                    continue
                mult = m.get("mult") or 1
                produtos.append({"produto": {
                    "produto_id": m["gc_id"], "quantidade": qty * mult,
                    "valor_venda": round(price / mult, 4),
                    "detalhes": f"Anota AI #{ref}: {it.get('name')}"}})
            if faltando:
                res["pendentes"].append({"ref": ref, "motivo": "produto sem cadastro: "
                                         + ", ".join(str(x) for x in faltando)})
                continue

            frete = float(det.get("deliveryFee") or 0)
            descontos = sum(float(x.get("amount") or x.get("value") or 0)
                            for x in det.get("discounts") or [])
            if descontos:
                produtos[0]["produto"]["tipo_desconto"] = "R$"
                produtos[0]["produto"]["desconto_valor"] = round(descontos, 2)
            total = float(det.get("total") or 0)
            calc = round(soma + frete - descontos, 2)
            if abs(calc - total) > 0.02:
                res["pendentes"].append({"ref": ref, "motivo": f"total não bate "
                                         f"(itens+frete−desconto = {calc:.2f} × app = {total:.2f})"})
                continue

            pagamentos, forma_nome = [], ""
            for p in det.get("payments", []):
                fid, fnome = payment_forma(p)
                if not fid:
                    pagamentos = None
                    res["pendentes"].append({"ref": ref, "motivo": "forma de pagamento "
                                             f"desconhecida: {p.get('code') or p.get('name')}"})
                    break
                forma_nome = forma_nome or fnome
                pagamentos.append({"pagamento": {
                    "data_vencimento": data_brt, "valor": round(float(p.get("value") or 0), 2),
                    "forma_pagamento_id": fid,
                    "observacao": f"Anota AI: {p.get('code') or p.get('name')}"}})
            if pagamentos is None:
                continue

            venda = {"tipo": "produto", "cliente_id": GC_CLIENTE_DELIVERY, "data": data_brt,
                     "situacao_id": GC_SITUACAO_CONCRETIZADA, "condicao_pagamento": "a_vista",
                     "valor_frete": round(frete, 2),
                     "observacoes": f"Importado do Anota AI — pedido #{ref} ({oid}), "
                                    f"canal {det.get('from')}/{det.get('salesChannel')}",
                     "pagamentos": pagamentos, "produtos": produtos}
            if not post:
                res["novos"].append({"ref": ref, "data": data_brt, "total": total, "venda": None})
                continue
            try:
                r = gcapi.post("/vendas", venda)
            except Exception as e:
                res["erros"].append({"ref": ref, "erro": str(e)[:180]})
                continue
            if r.get("status") != "success":
                res["erros"].append({"ref": ref, "erro": json.dumps(r, ensure_ascii=False)[:180]})
                continue
            vid = (r.get("data") or {}).get("id")
            refs.add(ref); oids.add(oid)
            nf = emit_nfce(vid, data_brt, pagamentos[0]["pagamento"]["forma_pagamento_id"])
            res["novos"].append({"ref": ref, "data": data_brt, "total": total,
                                 "venda": vid, "nfce": nf, "forma": forma_nome})
    except Exception as e:
        res["ok"] = False
        res["erros"].append({"ref": "-", "erro": str(e)[:200]})
    res["segundos"] = round(time.time() - ini, 1)
    return res

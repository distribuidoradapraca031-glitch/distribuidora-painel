#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mesma "cara" do gclient, mas /pagamentos e /recebimentos moram no GestãoGPT (virada de
30/10/2026). As telas do painel continuam falando em pagamento e recebimento; aqui isso vira
conta + parcela de lá. O resto (/vendas, /produtos, /compras) ainda passa pro gclient até migrar.

Conta de verdade: 1 parcela = 1 registro, id = id da parcela. A descrição inteira do painel e os
códigos que ele usa (plano, forma, conta) vão no OBS da conta, numa marca {{…}} no fim. O banco
de lá não guarda todo caractere ("—" volta como \\x14, "⟦" como "æ"): a marca é só ASCII e
_txt() desfaz o travessão na leitura. O que foi
lançado direto no GestãoGPT não tem marca: os códigos saem do plano e da conta corrente.

Registro só do painel ([RES±], [SOB±], [SALDO], [NI], [GELO…], valor zero) não pode virar conta,
senão aparece como conta a pagar na tela do dono: fica no controle (produtos técnicos inativos
CONTROLE PAINEL n, JSON até 3.500 letras por produto), com id "i<numero>".
"""
import json, re, threading, time, urllib.error, urllib.parse, urllib.request
import gclient as _gc
from gclient import _tok   # noqa: F401 (o app usa gcapi._tok)

API = "https://api.gestaogpt.com.br/v1"
GAVETA_GC, BANCO_GC = "696747", "681760"
CC_GAVETA, CC_BANCO = 2, 1
FORMA_DINHEIRO, FORMA_PIX = "6055919", "6055931"
FORMA_GG = {"6055919": "DINHEIRO", "6780142": "DINHEIRO", "6055931": "DEPOSITO", "6776761": "DEPOSITO",
            "6055920": "CARTAO", "6055921": "CARTAO", "6687681": "BOLETO"}
NOME_FORMA = {"6055919": "Dinheiro à Vista", "6780142": "Dinheiro guardado (reserva/sobra)", "6055931": "PIX",
              "6776761": "Recurso próprio Victor", "6055920": "Cartão de Crédito", "6055921": "Cartão de Débito",
              "6687681": "Boleto"}
# plano do GC -> conta analítica de lá (a lista deles é fixa e só leitura)
PLANO_GG = {"33015669": 102, "33015630": 80, "33015649": 71, "33015635": 117, "33015663": 101, "35981822": 7,
            "33015650": 10, "33015661": 104, "33015654": 101, "33015677": 118, "33015660": 78, "33015638": 56,
            "33015655": 114, "33015662": 106, "33015664": 103, "33015658": 118, "33015633": 112, "33015646": 79,
            "33015656": 106, "33015682": 106, "33015692": 94, "33015685": 87, "33015684": 87, "33015690": 94}
NOME_PLANO = {"33015669": "Compras", "33015682": "Ajuste de caixa", "33015635": "Contabilidade", "33015630": "Aluguel",
              "33015663": "Telefonia e internet", "33015649": "Energia elétrica + água", "35981822": "Impostos - DAS Simples Nacional",
              "33015660": "Remuneração funcionários", "33015638": "Distribuição de lucros", "33015655": "Limpeza",
              "33015662": "Supermercado", "33015664": "Transportadora", "33015658": "Material de escritório",
              "33015650": "Impostos - alvará", "33015633": "Combustivel e translados", "33015677": "Despesas bancárias",
              "33015646": "Encargos funcionários - INSS", "33015661": "Segurança", "33015654": "Licença ou aluguel de softwares",
              "33015656": "Manutenção equipamentos", "33015685": "Vendas no balcão", "33015692": "Ajuste de caixa",
              "33015684": "Vendas de produtos"}
PLANO_PADRAO_PAGAR, PLANO_PADRAO_RECEBER = 106, 94
INTERNO = re.compile(r"^\[(RES\+|RES-|SOB\+|SOB-|SALDO|NI|NIX|GELO[^\]]*)\]")
FORA_DA_LISTA = ("PDV DIF. ABERTURA", "PDV SANGRIA")   # abertura e fechamento: só a gaveta lê
MARCA = re.compile(r"\s*(?:æ|\{\{)(\{.*?\})(?:ç|\}\})\s*$", re.S)
CTRL_NOME = "CONTROLE PAINEL"
CTRL_MAX = 3400
_memo, _memo_lock = {}, threading.Lock()
_ctrl = {"ids": None}
_ctrl_txt = {}


def _req(metodo, caminho, corpo=None):
    h = {"Authorization": "Bearer " + _tok("GESTAOGPT_TOKEN"), "Accept": "application/json",
         "User-Agent": "DistribuidoraPraca031/1.0"}     # o Cloudflare deles barra o do Python
    if corpo is not None:
        h["Content-Type"] = "application/json"          # DELETE com Content-Type volta 400
    req = urllib.request.Request(API + caminho, method=metodo, headers=h,
                                 data=json.dumps(corpo).encode() if corpo is not None else None)
    for _ in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and metodo == "GET":
                time.sleep(int(e.headers.get("RateLimit-Reset") or 15) + 1)
                continue
            try:
                msg = json.load(e).get("message")
            except Exception:
                msg = None
            raise RuntimeError(msg or f"o GestãoGPT respondeu {e.code} em {caminho.split('?')[0]}") from None
    raise RuntimeError("o GestãoGPT está limitando as leituras agora")


def _todas(caminho):
    out, page, sep = [], 1, "&" if "?" in caminho else "?"
    while True:
        j = _req("GET", f"{caminho}{sep}count=100&page={page}")
        out += j.get("items") or []
        if not j.get("items") or len(out) >= (j.get("total") or 0):
            return out
        page += 1


def _txt(s):
    return (s or "").replace("\x14", "—")


def _limpa():
    with _memo_lock:
        _memo.clear()


def _guarda(chave, ttl, fn):
    with _memo_lock:
        hit = _memo.get(chave)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    v = fn()
    with _memo_lock:
        _memo[chave] = (time.time(), v)
    return v


# ---- controle: registros só do painel ----
def _ctrl_ids():
    if _ctrl["ids"] is None:
        q = urllib.parse.quote(CTRL_NOME)
        _ctrl["ids"] = sorted(p["ID"] for p in _todas(f"/produto?inativo=true&descricao={q}")
                              if (p.get("DESCRICAO") or "").startswith(CTRL_NOME))
    return _ctrl["ids"]


def _ctrl_ler():
    def ler():
        regs = {}
        for pid in _ctrl_ids():
            try:
                _ctrl_txt[pid] = _req("GET", f"/produto/{pid}").get("INFOADICIONAL") or "{}"
                regs.update(json.loads(_txt(_ctrl_txt[pid]), strict=False))
            except ValueError:
                pass
        return regs
    return _guarda("ctrl", 60, ler)


def _ctrl_grava(regs):
    """Reparte em páginas; cria produto técnico novo quando não cabe."""
    paginas, atual = [], {}
    for k in sorted(regs, key=lambda x: int(x[1:])):
        teste = dict(atual); teste[k] = regs[k]
        if len(json.dumps(teste, ensure_ascii=False, separators=(",", ":"))) > CTRL_MAX:
            paginas.append(atual); atual = {k: regs[k]}
        else:
            atual = teste
    paginas.append(atual)
    ids = _ctrl_ids()
    novos = len(ids) < len(paginas)
    while len(ids) < len(paginas):
        r = _req("POST", "/produto", {"descricao": f"{CTRL_NOME} {len(ids) + 1}", "codfiscal": f"CTRL-PAINEL-{len(ids) + 1}",
                                      "tipo": "SERVIÇO", "unidmed": "UN", "inativo": True, "infoadicional": "{}"})
        ids.append(r["ID"])
    for i, pid in enumerate(ids):
        txt = json.dumps(paginas[i] if i < len(paginas) else {}, ensure_ascii=False, separators=(",", ":"))
        if _ctrl_txt.get(pid) != txt:       # só regrava a página que mudou
            _req("PUT", f"/produto/{pid}", {"infoadicional": txt})
            _ctrl_txt[pid] = txt
    _limpa()


def _ctrl_novo(tipo, body):
    regs = dict(_ctrl_ler())
    n = max([int(k[1:]) for k in regs] or [0]) + 1
    k = f"i{n}"
    regs[k] = {"t": tipo, "d": body.get("descricao") or "", "v": str(body.get("valor") or "0"),
               "ve": (body.get("data_vencimento") or "")[:10], "c": (body.get("data_competencia") or "")[:10],
               "l": str(body.get("liquidado") or "0"), "li": (body.get("data_liquidacao") or "")[:10],
               "p": str(body.get("plano_contas_id") or ""), "cb": str(body.get("conta_bancaria_id") or ""),
               "f": str(body.get("forma_pagamento_id") or "")}
    _ctrl_grava(regs)
    return k


def _ctrl_shape(k, r):
    return {"id": k, "descricao": r["d"], "valor": r["v"], "valor_total": r["v"], "liquidado": r["l"],
            "data_vencimento": r["ve"], "data_competencia": r["c"] or r["ve"], "data_liquidacao": r["li"],
            "plano_contas_id": r["p"], "nome_plano_conta": NOME_PLANO.get(r["p"], ""), "conta_bancaria_id": r["cb"],
            "forma_pagamento_id": r["f"], "nome_forma_pagamento": NOME_FORMA.get(r["f"], ""),
            "fornecedor_id": "", "nome_fornecedor": "", "cadastrado_em": r["ve"], "interno": True}


# ---- conta de verdade ----
def _marca(body, descricao):
    partes = {"p": body.get("plano_contas_id"), "f": body.get("forma_pagamento_id"),
              "c": body.get("conta_bancaria_id"), "fo": body.get("fornecedor_id"), "nf": body.get("nome_fornecedor"),
              "dc": (body.get("data_competencia") or "")[:10]}
    m = json.dumps({k: str(v) for k, v in partes.items() if v}, ensure_ascii=False, separators=(",", ":"))
    return f"{descricao[:500 - len(m) - 6]} {{{{{m}}}}}"


def _fatura(desc):
    """O nome que o dono vê na tela do GestãoGPT, sem as marcas internas do painel."""
    return re.sub(r"\[[^\]]*\]\s*|\s*#ext:\S+", "", desc).strip()[:50] or "Lançamento"


def _shape(p, tipo):
    k = p.get("CONTA") or {}
    obs = _txt(k.get("OBS"))
    m = MARCA.search(obs)
    meta = {}
    if m:
        try:
            meta = json.loads(m.group(1))
        except ValueError:
            pass
    desc = MARCA.sub("", obs).strip() if m else (_txt(k.get("FATURA")) or obs or "").strip()
    cc = p.get("ID_CONTACORRENTE")
    forma = meta.get("f") or (FORMA_DINHEIRO if cc == CC_GAVETA else FORMA_PIX if p.get("STATUS") == "PG" else "6687681")
    pago = p.get("STATUS") == "PG"
    v = f"{float(p.get('VALOR_PAGO') if pago and p.get('VALOR_PAGO') else p.get('VALOR') or 0):.2f}"
    venc = (p.get("DATA_VENC") or "")[:10]
    return {"id": str(p["ID"]), "descricao": desc, "valor": v, "valor_total": v, "liquidado": "1" if pago else "0",
            "data_vencimento": venc, "data_competencia": meta.get("dc") or venc,
            "data_liquidacao": (p.get("DATA_PAGTO") or "")[:10], "plano_contas_id": meta.get("p") or "",
            "nome_plano_conta": NOME_PLANO.get(meta.get("p") or "") or k.get("PLNCONTA_DESCRICAO") or "",
            "conta_bancaria_id": meta.get("c") or (GAVETA_GC if cc == CC_GAVETA else BANCO_GC), "forma_pagamento_id": forma,
            "nome_forma_pagamento": NOME_FORMA.get(forma, p.get("FORMAPAGTO") or ""),
            "fornecedor_id": meta.get("fo") or "", "nome_fornecedor": meta.get("nf") or "",
            "cadastrado_em": (k.get("DATA_LANC") or venc)[:10], "_conta": k.get("ID")}


def _visivel(p):
    k = p.get("CONTA") or {}
    if (k.get("ORIGEM") or "").strip() == "CLI" or k.get("ID_PEDIDO"):
        return False        # venda e fiado não são pagamento nem recebimento avulso
    return not any(t in (k.get("FATURA") or "").upper() for t in FORA_DA_LISTA)


def _lista(path, params):
    tipo = "pagar" if path == "/pagamentos" else "receber"
    de, ate = (params or {}).get("data_inicio") or "", (params or {}).get("data_fim") or ""
    q = f"/conta-parcela?tipo={tipo}&status=todos"
    if de:
        q += f"&data_venc_inicio={de}"
    if ate:
        q += f"&data_venc_fim={ate}"
    out = [_shape(p, tipo) for p in _guarda(q, 30, lambda: _todas(q)) if _visivel(p)]
    for k, r in _ctrl_ler().items():
        if r["t"] == tipo and (not de or r["ve"] >= de) and (not ate or r["ve"] <= ate):
            out.append(_ctrl_shape(k, r))
    return out


def _cc(body):
    return CC_GAVETA if str(body.get("conta_bancaria_id")) == GAVETA_GC else CC_BANCO


def _plano(body, tipo):
    return PLANO_GG.get(str(body.get("plano_contas_id") or "")) or (PLANO_PADRAO_PAGAR if tipo == "pagar" else PLANO_PADRAO_RECEBER)


def _post(path, body):
    tipo = "pagar" if path == "/pagamentos" else "receber"
    desc = (body.get("descricao") or "").strip()
    valor = float(body.get("valor") or 0)
    if INTERNO.match(desc) or valor <= 0:
        return {"code": 200, "status": "success", "data": {"id": _ctrl_novo(tipo, body)}}
    venc = (body.get("data_vencimento") or time.strftime("%Y-%m-%d"))[:10]
    forma = FORMA_GG.get(str(body.get("forma_pagamento_id")))
    parc = {"data_venc": venc, "valor": round(valor, 2), "id_conta_corrente": _cc(body), "id_plnconta": _plano(body, tipo)}
    if forma:
        parc["forma_pagto"] = forma
    r = _req("POST", "/conta", {"tipo": tipo, "origem": "OUT", "id_plnconta": _plano(body, tipo), "fatura": _fatura(desc),
                                "obs": _marca(body, desc), "data_lanc": (body.get("cadastrado_em") or time.strftime("%Y-%m-%d"))[:10],
                                "parcelas": [parc]})
    pid = r["parcelas"][0]["ID"]
    if str(body.get("liquidado")) == "1":
        _req("POST", f"/conta-parcela/{pid}/baixar", {"data_pagto": (body.get("data_liquidacao") or venc)[:10],
                                                      "id_conta_corrente": _cc(body)})
    _limpa()
    return {"code": 200, "status": "success", "data": {"id": str(pid)}}


def _put(path, rid, body):
    tipo = "pagar" if path == "/pagamentos" else "receber"
    if rid.startswith("i"):
        regs = dict(_ctrl_ler())
        if rid not in regs:
            raise RuntimeError("registro não encontrado")
        r = regs[rid]
        r.update({"d": body.get("descricao", r["d"]), "v": str(body.get("valor", r["v"])),
                  "ve": (body.get("data_vencimento") or r["ve"])[:10], "c": (body.get("data_competencia") or r["c"])[:10],
                  "l": str(body.get("liquidado", r["l"])), "li": (body.get("data_liquidacao") or r["li"])[:10],
                  "p": str(body.get("plano_contas_id", r["p"])), "cb": str(body.get("conta_bancaria_id", r["cb"])),
                  "f": str(body.get("forma_pagamento_id", r["f"]))})
        _ctrl_grava(regs)
        return {"code": 200, "status": "success", "data": {"id": rid}}
    p = _req("GET", f"/conta-parcela/{rid}")
    pago_antes, pago_agora = p.get("STATUS") == "PG", str(body.get("liquidado")) == "1"
    if pago_antes:          # parcela paga só muda valor e conta depois de estornar
        _req("POST", f"/conta-parcela/{rid}/estornar", {})
    desc = (body.get("descricao") or "").strip()
    forma = FORMA_GG.get(str(body.get("forma_pagamento_id")))
    campos = {"data_venc": (body.get("data_vencimento") or p.get("DATA_VENC"))[:10], "id_conta_corrente": _cc(body)}
    if body.get("valor") not in (None, ""):
        campos["valor"] = round(float(body["valor"]), 2)
    if forma:
        campos["forma_pagto"] = forma
    _req("PUT", f"/conta-parcela/{rid}", campos)
    _req("PUT", f"/conta/{p['ID_CONTA']}", {"fatura": _fatura(desc), "obs": _marca(body, desc)})
    if pago_agora:
        _req("POST", f"/conta-parcela/{rid}/baixar", {"data_pagto": (body.get("data_liquidacao") or time.strftime("%Y-%m-%d"))[:10],
                                                      "id_conta_corrente": _cc(body)})
    _limpa()
    return {"code": 200, "status": "success", "data": {"id": rid}}


def _delete(rid):
    if rid.startswith("i"):
        regs = dict(_ctrl_ler())
        regs.pop(rid, None)
        _ctrl_grava(regs)
        return {"code": 200, "status": "success"}
    p = _req("GET", f"/conta-parcela/{rid}")
    if p.get("STATUS") == "PG":
        _req("POST", f"/conta-parcela/{rid}/estornar", {})
    irmas = _req("GET", f"/conta/{p['ID_CONTA']}").get("parcelas") or []
    _req("DELETE", f"/conta/{p['ID_CONTA']}" if len(irmas) <= 1 else f"/conta-parcela/{rid}")
    _limpa()
    return {"code": 200, "status": "success"}


# ---- vendas ----
VIRADA = "2026-10-30"


def _vendas(params):
    """Vendas no formato do GC. Sem produtos: isso exige abrir venda por venda, e quem precisa
    (curva ABC) ainda vai migrar. Antes da virada, tenta o GC enquanto ele responder."""
    params = params or {}
    tipo, de, ate = params.get("tipo"), (params.get("data_inicio") or VIRADA), (params.get("data_fim") or "2999-12-31")
    out = []
    if de < VIRADA:
        try:
            out = _gc.get_all("/vendas", {**params, "data_fim": min(ate, "2026-10-29")})
        except Exception:
            out = []
    if ate < VIRADA:
        return out
    ini = max(de, VIRADA)
    peds = _guarda(f"ped{ini}{ate}", 30, lambda: _todas(f"/pedido?situacao=todos&pdv=todos&data_de={ini}&data_ate={ate}"))
    parc = _guarda("parc_rec", 30, lambda: _todas(f"/conta-parcela?tipo=receber&status=todos&data_venc_inicio={ini}"))
    por = {}
    for p in parc:
        k = p.get("CONTA") or {}
        if k.get("ID_PEDIDO"):
            por.setdefault(k["ID_PEDIDO"], []).append(p)
    for v in peds:
        if not v.get("EMITIDO"):
            continue
        ps = por.get(v["ID"]) or []
        ref = next(((p.get("CONTA") or {}).get("REFBUSCA") or "" for p in ps), "")
        anota = ref.startswith("ANOTA-")
        if (tipo == "vendas_balcao" and not v.get("PDV")) or (tipo == "produto" and not anota):
            continue
        out.append({"id": str(v["ID"]), "data": v["DATA"][:10], "cadastrado_em": v["DATA"][:19].replace("T", " "),
                    "valor_total": f"{float(v.get('TOTAL') or 0):.2f}", "nome_situacao": "Concretizada",
                    "observacoes": f"Importado do Anota AI — pedido #{ref[6:]}" if anota else "", "produtos": [],
                    "pagamentos": [{"pagamento": {"valor": f"{float(p.get('VALOR') or 0):.2f}",
                                                  "nome_forma_pagamento": re.sub(r"^\d+-", "", p.get("FORMAPAGTO") or "")}} for p in ps]})
    return out


def _rota(path):
    m = re.match(r"^(/pagamentos|/recebimentos)(?:/([^/?]+))?$", path)
    return (m.group(1), m.group(2)) if m else (None, None)


def get(path, params=None):
    base, rid = _rota(path)
    if not base:
        return _gc.get(path, params)
    if rid:
        if rid.startswith("i"):
            r = _ctrl_ler().get(rid)
            return {"code": 200, "status": "success", "data": _ctrl_shape(rid, r) if r else {}}
        return {"code": 200, "status": "success", "data": _shape(_req("GET", f"/conta-parcela/{rid}"), base)}
    return {"code": 200, "status": "success", "data": _lista(base, params), "meta": {}}


def get_all(path, params=None, hard_cap=5000):
    base, _ = _rota(path)
    if path == "/vendas":
        return _vendas(params)
    return _lista(base, params) if base else _gc.get_all(path, params, hard_cap=hard_cap)


def post(path, body):
    base, _ = _rota(path)
    return _post(base, body) if base else _gc.post(path, body)


def put(path, body):
    base, rid = _rota(path)
    return _put(base, rid, body) if base else _gc.put(path, body)


def delete(path):
    base, rid = _rota(path)
    return _delete(rid) if base else _gc.delete(path)

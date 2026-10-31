#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mesma "cara" do gclient, mas /pagamentos e /recebimentos moram no GestãoGPT (virada de
30/10/2026). As telas do painel continuam falando em pagamento e recebimento; aqui isso vira
conta + parcela de lá. /produtos, /compras e /fornecedores também moram lá (ver as seções abaixo).

Conta de verdade: 1 parcela = 1 registro, id = id da parcela. A descrição inteira do painel e os
códigos que ele usa (plano, forma, conta) vão no OBS da conta, numa marca {{…}} no fim. O banco
de lá não guarda todo caractere ("—" volta como \\x14, "⟦" como "æ"): a marca é só ASCII e
_txt() desfaz o travessão na leitura. O que foi
lançado direto no GestãoGPT não tem marca: os códigos saem do plano e da conta corrente.

Registro só do painel ([RES±], [SOB±], [SALDO], [NI], [GELO…], valor zero) não pode virar conta,
senão aparece como conta a pagar na tela do dono: fica no controle (produtos técnicos inativos
CONTROLE PAINEL n, um JSON cortado em pedaços de 2.400 letras), com id "i<numero>".
"""
import base64, json, os, re, threading, time, urllib.error, urllib.parse, urllib.request, zlib
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
FORA_DA_LISTA = ("PDV DIF.", "PDV SANGRIA")   # abertura e fechamento: só a gaveta lê
MARCA = re.compile(r"\s*(?:æ|\{\{)(\{.*?\})(?:ç|\}\})\s*$", re.S)
CTRL_NOME = "CONTROLE PAINEL"
HIST_NOME = "CONTROLE HIST GC"
CTRL_MAX = 2400     # o gatilho de auditoria deles soma texto antigo + novo e estoura em 5.000 (erro 500)
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
    """Os registros são um JSON só, cortado em pedaços de 2.400 letras, um por produto técnico."""
    def ler():
        txt = ""
        for pid in _ctrl_ids():
            _ctrl_txt[pid] = _req("GET", f"/produto/{pid}").get("INFOADICIONAL") or ""
            txt += _ctrl_txt[pid]
        return json.loads(_txt(txt), strict=False) if txt.strip() else {}
    return _guarda("ctrl", 120, ler)


def _ctrl_grava(regs):
    """Só regrava o pedaço que mudou; cria produto técnico novo quando não cabe."""
    txt = json.dumps(dict(sorted(regs.items(), key=lambda kv: int(kv[0][1:]))), ensure_ascii=False, separators=(",", ":"))
    pedacos = [txt[i:i + CTRL_MAX] for i in range(0, len(txt), CTRL_MAX)]
    ids = _ctrl_ids()
    while len(ids) < len(pedacos):
        r = _req("POST", "/produto", {"descricao": f"{CTRL_NOME} {len(ids) + 1}", "codfiscal": f"CTRL-PAINEL-{len(ids) + 1}",
                                      "tipo": "SERVIÇO", "unidmed": "UN", "inativo": True, "infoadicional": ""})
        ids.append(r["ID"])
    for i, pid in enumerate(ids):
        t = pedacos[i] if i < len(pedacos) else ""
        if _ctrl_txt.get(pid) != t:
            _req("PUT", f"/produto/{pid}", {"infoadicional": t})
            _ctrl_txt[pid] = t
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
    velho = _shape(p, tipo)      # o painel reescreve a conta sem o fornecedor: não pode perder
    body = {**body, "fornecedor_id": body.get("fornecedor_id") or velho["fornecedor_id"],
            "nome_fornecedor": body.get("nome_fornecedor") or velho["nome_fornecedor"]}
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


# ---- histórico do GC, congelado aqui (scripts/gestaogpt_congela_hist.py) ----
# O GC venceu: nada aqui pergunta mais pra ele. Um erro ao ler sobe de propósito, assim os
# gráficos ficam com a última foto boa em vez de redesenhar o ano sem o histórico.
def hist_ids():
    q = urllib.parse.quote(HIST_NOME)
    return sorted(p["ID"] for p in _todas(f"/produto?inativo=true&descricao={q}") if (p.get("DESCRICAO") or "").startswith(HIST_NOME))


def hist_gc():
    def ler():
        txt = "".join(_req("GET", f"/produto/{pid}").get("INFOADICIONAL") or "" for pid in hist_ids())
        return json.loads(zlib.decompress(base64.b64decode(txt)))
    return _guarda("hist_gc", 21600, ler)


def _hist_vendas(tipo, de, ate):
    """Um registro por dia e tipo, no formato do GC; _n é quantas vendas ele resume e _cmv o custo delas."""
    out = []
    for g in hist_gc()["vendas"]:
        if not de <= g["dia"] <= ate or (tipo and tipo != {"b": "vendas_balcao", "p": "produto"}[g["tp"]]):
            continue
        prods = [{"produto": {"produto_id": pid, "nome_produto": "", "quantidade": str(q), "valor_total": str(fat),
                              "valor_venda": str(round(fat / q, 4) if q else 0)}} for pid, (q, fat) in g["it"].items()]
        if g["sq"]:
            prods.append({"produto": {"produto_id": "", "nome_produto": "SAQUE", "quantidade": "0", "valor_total": "0", "valor_venda": "0"}})
        out.append({"id": f"h{g['dia']}{g['tp']}{g['sq']}", "data": g["dia"], "cadastrado_em": g["dia"] + " 12:00:00", "valor_total": str(g["t"]),
                    "valor_produtos": str(g["vp"]), "valor_frete": str(g["f"]), "desconto_valor": str(g["d"]), "nome_situacao": "Concretizada",
                    "observacoes": "Anota AI" if g["tp"] == "p" else "", "_n": g["n"], "_cmv": g["c"], "produtos": prods,
                    "pagamentos": [{"pagamento": {"valor": str(v), "nome_forma_pagamento": k}} for k, v in g["pg"].items()]})
    return out


def pagamentos_com_hist(params):
    """Contas de agora + as pagas antes de outubro, que ficaram só na cópia do GC."""
    return _lista("/pagamentos", params) + hist_gc()["pagos"]


def _vendas(params):
    """Vendas no formato do GC. Sem produtos: isso exige abrir venda por venda; quem precisa
    (curva ABC, gráficos) soma as saídas de estoque em saidas_de_venda. Antes da virada, o histórico."""
    params = params or {}
    tipo, de, ate = params.get("tipo"), (params.get("data_inicio") or VIRADA), (params.get("data_fim") or "2999-12-31")
    out = []
    if de < VIRADA:
        out = _hist_vendas(tipo, de, min(ate, "2026-10-29"))
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
                    "valor_produtos": f"{float(v.get('TOTAL') or 0) - float(v.get('VALOR_FRETE') or 0):.2f}",
                    "valor_frete": f"{float(v.get('VALOR_FRETE') or 0):.2f}", "desconto_valor": "0",
                    "observacoes": f"Importado do Anota AI — pedido #{ref[6:]}" if anota else "", "produtos": [],
                    "pagamentos": [{"pagamento": {"valor": f"{float(p.get('VALOR') or 0):.2f}",
                                                  "nome_forma_pagamento": re.sub(r"^\d+-", "", p.get("FORMAPAGTO") or "")}} for p in ps]})
    return out


# ---- produtos ----
# Por fora o id continua o do GC (gelo, mapa do Anota, histórico de compras dependem dele); produto
# novo usa o id do GestãoGPT. Fardo e fornecedor não têm campo lá: ficam em produtos técnicos.
# INFOADICIONAL pode sair impresso na nota, por isso o [fardo=N] não vai no produto.
_DIR = os.path.dirname(os.path.abspath(__file__))
_MAPA = json.load(open(os.path.join(_DIR, "ggpt_map.json")))
_INV = {v: k for k, v in _MAPA.items()}
_SEM_ESTOQUE = set(json.load(open(os.path.join(_DIR, "ggpt_sem_estoque.json"))))
_tec = {}


def _ext(pid):
    return _INV.get(pid) or str(pid)


def _pid(ext):
    return _MAPA.get(str(ext)) or int(ext)


def _tec_id(nome, criar=False):
    if nome not in _tec:
        q = urllib.parse.quote(nome)
        ids = [p["ID"] for p in _todas(f"/produto?inativo=true&descricao={q}") if (p.get("DESCRICAO") or "").strip().upper() == nome]
        if ids:
            _tec[nome] = ids[0]
        elif criar:
            _tec[nome] = _req("POST", "/produto", {"descricao": nome, "codfiscal": "CTRL-" + nome[9:18].strip().replace(" ", "-"), "tipo": "SERVIÇO",
                                                   "unidmed": "UN", "inativo": True, "infoadicional": "{}"})["ID"]
        else:
            return None
    return _tec[nome]


def _tec_ler(nome):
    def ler():
        pid = _tec_id(nome)
        try:
            return json.loads(_txt(_req("GET", f"/produto/{pid}").get("INFOADICIONAL")) or "{}", strict=False) if pid else {}
        except ValueError:
            return {}
    return _guarda("tec" + nome, 60, ler)


def _tec_grava(nome, dado):
    _req("PUT", f"/produto/{_tec_id(nome, True)}", {"infoadicional": json.dumps(dado, ensure_ascii=False, separators=(",", ":"))})
    _limpa()


def _p_shape(p):
    ext, nome = _ext(p["ID"]), (p.get("DESCRICAO") or "").strip()
    fardo = _tec_ler("CONTROLE FARDOS").get(ext)
    custo, venda = f"{float(p.get('CUSTO') or 0):.4f}", f"{float(p.get('VALORUNIT') or 0):.2f}"
    tecnico = nome.startswith("CONTROLE ")
    return {"id": ext, "nome": nome, "codigo_interno": p.get("CODFISCAL") or "", "codigo_barra": p.get("EAN") or "",
            "ativo": "0" if p.get("INATIVO") else "1", "estoque": str(p.get("ESTOQUE") or 0),
            "movimenta_estoque": "0" if ext in _SEM_ESTOQUE or tecnico else "1", "valor_custo": custo, "valor_venda": venda,
            "grupo_id": str(p.get("ID_GRUPO") or ""), "nome_grupo": p.get("GRUPO") or "",
            "descricao": _txt(p.get("INFOADICIONAL")) if tecnico else (f"[fardo={fardo}]" if fardo else ""),
            "fiscal": {"ncm": p.get("CLFISC") or ""}, "possui_composicao": "0", "possui_variacao": "0",
            "valores": [{"nome_tipo": "Varejo", "valor_custo": custo, "valor_venda": venda}]}


def _produtos(params):
    nome = ((params or {}).get("nome") or "").strip()
    if nome.startswith("CONTROLE "):
        pid = _tec_id(nome.upper())
        return [_p_shape(_req("GET", f"/produto/{pid}"))] if pid else []
    return [_p_shape(p) for p in _guarda("prods", 30, lambda: _todas("/produto"))]


def _p_post(body):
    tecnico = str(body.get("ativo")) == "0"
    r = _req("POST", "/produto", {"descricao": body.get("nome"), "tipo": "SERVIÇO" if tecnico else "PRODUTO", "unidmed": "UN", "inativo": tecnico,
                                  "custo": float(body.get("valor_custo") or 0), "valorunit": float(body.get("valor_venda") or 0),
                                  **({"infoadicional": body.get("descricao") or "{}"} if tecnico else {})})
    _limpa()
    return {"code": 200, "status": "success", "data": {"id": str(r["ID"])}}


def _p_put(ext, body):
    pid = _pid(ext)
    atual = _req("GET", f"/produto/{pid}")
    if (atual.get("DESCRICAO") or "").startswith("CONTROLE "):
        _req("PUT", f"/produto/{pid}", {"infoadicional": body.get("descricao") or "{}"})
        _limpa()
        return {"code": 200, "status": "success", "data": {"id": ext}}
    campos = {}
    if body.get("valor_custo") not in (None, ""):
        campos["custo"] = round(float(body["valor_custo"]), 4)
    if body.get("valor_venda") not in (None, ""):
        campos["valorunit"] = round(float(body["valor_venda"]), 2)
    if body.get("nome") and body["nome"].strip() != (atual.get("DESCRICAO") or "").strip():
        campos["descricao"] = body["nome"].strip()
    if campos:
        _req("PUT", f"/produto/{pid}", campos)
    if "descricao" in body:
        m = re.search(r"\[fardo=(\d+(?:[.,]\d+)?)\]", body.get("descricao") or "", re.I)
        f = dict(_tec_ler("CONTROLE FARDOS"))
        if m:
            f[str(ext)] = int(float(m.group(1).replace(",", ".")))
        else:
            f.pop(str(ext), None)
        _tec_grava("CONTROLE FARDOS", f)
    if body.get("estoque") not in (None, ""):
        delta = round(float(body["estoque"]) - float(atual.get("ESTOQUE") or 0), 4)
        if abs(delta) >= 0.0005:
            _req("POST", f"/produto/{pid}/estoque/movimentar", {"tipo": "entrada" if delta > 0 else "saida", "qtde": abs(delta),
                                                                "obs": "Acerto de estoque pelo painel"})
    _limpa()
    return {"code": 200, "status": "success", "data": {"id": ext}}


# ---- compras ----
# A API do GestãoGPT não tem compra. Compra = entrada de estoque + conta(s) "Compra de nº X" + registro
# no controle, que é o que o histórico da tela lê e o que permite desfazer.
def _un_linha(p):
    m = re.search(r"=\s*([\d.,]+)\s*un\b", p.get("detalhes") or "", re.I)
    return float(m.group(1).replace(",", ".")) if m else float(p.get("quantidade") or 0)


def _c_shape(k, j):
    return {**j, "id": k, "produtos": [{"produto": x} for x in j.get("produtos") or []],
            "pagamentos": [{"pagamento": x} for x in j.get("pagamentos") or []]}


def _compras():
    return [_c_shape(k, r["j"]) for k, r in _ctrl_ler().items() if r["t"] == "compra"]


def _c_post(body):
    cod, data = str(body.get("codigo") or ""), (body.get("data_emissao") or time.strftime("%Y-%m-%d"))[:10]
    forn = str(body.get("fornecedor_id") or "")
    nome_f = next((f["nome"] for f in _fornecedores() if str(f["id"]) == forn), "")
    linhas, pags, nomes, erro = [], [], {}, None
    try:
        for w in body.get("produtos") or []:
            p = w.get("produto", w)
            un, vt = _un_linha(p), float(p.get("valor_total") or 0)
            prod = _req("GET", f"/produto/{_pid(p['produto_id'])}")
            _req("POST", f"/produto/{_pid(p['produto_id'])}/estoque/movimentar",
                 {"tipo": "entrada", "qtde": un, "valor": round(vt / un, 4) if un else 0, "obs": f"Compra nº {cod}"[:60]})
            linhas.append({"produto_id": str(p["produto_id"]), "nome_produto": (prod.get("DESCRICAO") or "").strip(), "quantidade": p.get("quantidade"),
                           "valor_custo": p.get("valor_custo"), "valor_total": vt, "detalhes": p.get("detalhes") or ""})
        for w in body.get("pagamentos") or []:
            pg = w.get("pagamento", w)
            _post("/pagamentos", {"descricao": f"Compra de nº {cod}", "valor": f"{float(pg.get('valor') or 0):.2f}", "data_vencimento": pg.get("data_vencimento") or data,
                                  "data_competencia": data, "liquidado": pg.get("liquidado", "0"), "data_liquidacao": pg.get("data_liquidacao") or "",
                                  "plano_contas_id": pg.get("plano_contas_id") or "33015669", "conta_bancaria_id": pg.get("conta_bancaria_id") or BANCO_GC,
                                  "forma_pagamento_id": pg.get("forma_pagamento_id"), "fornecedor_id": forn, "nome_fornecedor": nome_f, "cadastrado_em": data})
            pags.append({"nome_forma_pagamento": NOME_FORMA.get(str(pg.get("forma_pagamento_id")), ""), "valor": pg.get("valor")})
    except Exception as e:
        erro = str(e)[:150]
    regs = dict(_ctrl_ler())
    k = f"i{max([int(x[1:]) for x in regs] or [0]) + 1}"
    regs[k] = {"t": "compra", "j": {"codigo": cod, "data_emissao": data, "fornecedor_id": forn, "nome_fornecedor": nome_f, "nome_situacao": "Confirmada",
                                    "valor_total": body.get("valor_total"), "numero_nfe": body.get("numero_nfe") or "", "produtos": linhas, "pagamentos": pags}}
    _ctrl_grava(regs)
    if erro:
        raise RuntimeError(f"a compra {cod} parou no meio ({erro}). Entrou o que está no histórico: confira antes de lançar de novo.")
    return {"code": 200, "status": "success", "data": {"id": k, "codigo": cod}}


def _c_delete(k):
    regs = dict(_ctrl_ler())
    r = regs.get(k)
    if not r:
        raise RuntimeError("compra não encontrada")
    j = r["j"]
    for x in j.get("produtos") or []:
        _req("POST", f"/produto/{_pid(x['produto_id'])}/estoque/movimentar", {"tipo": "saida", "qtde": _un_linha(x), "obs": f"Desfaz compra nº {j.get('codigo')}"[:60]})
    for p in _lista("/pagamentos", {"data_inicio": "2026-01-01", "data_fim": "2027-12-31"}):
        if p["descricao"] == f"Compra de nº {j.get('codigo')}":
            _delete(p["id"])
    regs = dict(_ctrl_ler())
    regs.pop(k, None)
    _ctrl_grava(regs)
    return {"code": 200, "status": "success"}


def _fornecedores():
    return _tec_ler("CONTROLE FORNECEDORES").get("itens") or []


def _forn_post(body):
    """A API de lá não cria fornecedor: o novo fica só na lista do painel, como os outros."""
    nome = (body.get("nome") or "").strip().upper()
    if not nome:
        raise RuntimeError("fornecedor sem nome")
    itens = list(_fornecedores())
    achou = next((f for f in itens if (f.get("nome") or "").upper() == nome), None)
    if achou:
        return {"code": 200, "status": "success", "data": {"id": achou["id"], "nome": achou["nome"]}}
    fid = "n" + str(1 + max([int(f["id"][1:]) for f in itens if str(f["id"]).startswith("n")] or [0]))
    itens.append({"id": fid, "nome": nome, "ativo": "1"})
    _tec_grava("CONTROLE FORNECEDORES", {"itens": itens})
    return {"code": 200, "status": "success", "data": {"id": fid, "nome": nome}}


def movs_venda():
    """Saídas de venda (caixa e delivery) desde a virada, em ordem. Cancelou, entra negativo. A receita
    sai como Saida da venda, entre a saída do composto e a "Entrada Produto Composto" dele: fica de fora."""
    movs = sorted(_guarda("movs", 300, lambda: _todas("/estoque/movimentacoes")), key=lambda m: m["ID"])
    ingrediente = set()
    for i, m in enumerate(movs):
        if m.get("ORIGEM") == "Produto Composto":
            for j in range(i - 1, -1, -1):
                if movs[j].get("ID_PRODUTO") == m.get("ID_PRODUTO"):
                    break
                ingrediente.add(movs[j]["ID"])
    out = []
    for m in movs:
        dia, tipo = (m.get("DATA") or "")[:10], m.get("TIPOMOV")
        if dia < VIRADA or "Venda" not in (m.get("ORIGEM") or "") or tipo not in ("Saida", "Estorno") or m["ID"] in ingrediente:
            continue
        q = (1 if tipo == "Saida" else -1) * float(m.get("QTDE") or 0)
        out.append({"id": m["ID"], "pid": _ext(m.get("ID_PRODUTO")), "dia": dia, "qtde": q, "valor": q * float(m.get("VALOR") or 0)})
    return out


def saidas_de_venda(desde):
    """Unidades vendidas por produto desde a virada, pelas movimentações de estoque."""
    return [m for m in movs_venda() if m["dia"] >= desde]


def receitas_status():
    """Quantos copões, doses e combos já baixam a receita: a API não deixa marcar PRODUTO COMPOSTO."""
    mapa = json.load(open(os.path.join(_DIR, "mapa_drinks.json")))
    ids = {_pid(k) for k in mapa}
    prods = [p for p in _guarda("prod_tipo", 300, lambda: _todas("/produto")) if p["ID"] in ids]
    return {"total": len(ids), "ativas": sum(1 for p in prods if p.get("TIPO") == "PRODUTO COMPOSTO")}


def _rota(path):
    m = re.match(r"^(/pagamentos|/recebimentos)(?:/([^/?]+))?$", path)
    return (m.group(1), m.group(2)) if m else (None, None)


def get(path, params=None):
    base, rid = _rota(path)
    if not base:
        m = re.match(r"^/(produtos|compras)/([^/?]+)$", path)
        if m and m.group(1) == "produtos":
            return {"code": 200, "status": "success", "data": _p_shape(_req("GET", f"/produto/{_pid(m.group(2))}"))}
        if m:
            r = _ctrl_ler().get(m.group(2))
            return {"code": 200, "status": "success", "data": _c_shape(m.group(2), r["j"]) if r else {}}
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
    if path == "/produtos":
        return _produtos(params)
    if path == "/compras":
        return _compras()
    if path == "/fornecedores":
        return _fornecedores()
    return _lista(base, params) if base else _gc.get_all(path, params, hard_cap=hard_cap)


def post(path, body):
    if path == "/produtos":
        return _p_post(body)
    if path == "/compras":
        return _c_post(body)
    if path == "/fornecedores":
        return _forn_post(body)
    base, _ = _rota(path)
    if not base:
        raise RuntimeError(f"gravação sem destino no GestãoGPT: {path}")   # nada grava mais no GC
    return _post(base, body)


def put(path, body):
    m = re.match(r"^/(produtos|compras)/([^/?]+)$", path)
    if m:       # compra: reabrir antes de excluir era coisa do GC, aqui não faz nada
        return _p_put(m.group(2), body) if m.group(1) == "produtos" else {"code": 200, "status": "success"}
    base, rid = _rota(path)
    if not base:
        raise RuntimeError(f"gravação sem destino no GestãoGPT: {path}")
    return _put(base, rid, body)


def delete(path):
    m = re.match(r"^/compras/([^/?]+)$", path)
    if m:
        return _c_delete(m.group(1))
    base, rid = _rota(path)
    if not base:
        raise RuntimeError(f"exclusão sem destino no GestãoGPT: {path}")
    return _delete(rid)

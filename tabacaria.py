#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Baixa no estoque o MAÇO que virou cigarro avulso, fumo picado ou seda.

Mesma ideia da baixa das garrafas (drinks.py) e mesmo lugar: dentro do webapp, pra
rodar igual no Mac e na NUVEM sem duas versões pra divergir.

Porção não é produto de prateleira (movimenta_estoque=0) — quem tem estoque de
verdade é o MAÇO (ou a CAIXA, no caso da seda). Regra da casa:

    1 maço  = 20 porções (cigarro avulso ou fumo picado)
    1 caixa = 33 sedas

REGRA DA CASA: igual ao whisky, o maço só sai do estoque INTEIRO, quando as porções
vendidas fecham um maço. O que sobra fica de saldo e entra na próxima rodada — assim
o estoque não fica quebrado (3,95 maços) como ficava na versão que rodava na mão.

O cigarro avulso não diz a marca (o SKU é só o preço: "CIGARRO PICADO 2,00"), então
ele é rateado entre as marcas de maço na proporção das COMPRAS dos últimos 90 dias:
o que o dono repõe é o que a loja consome.

ONDE FICA O CONTROLE: num produto técnico inativo do próprio GestãoClick
("CONTROLE BAIXA TABACARIA"), no campo descrição. Não pode ser arquivo em disco: na
nuvem o disco é apagado a cada reinício e, sem o registro, a rotina reprocessaria
venda já baixada e descontaria o estoque EM DOBRO. O corte é o `cadastrado_em` da
venda (traz hora), então venda lançada com data retroativa também entra.
"""
import datetime
import json
from collections import defaultdict

POR_MACO = 20
POR_CAIXA_SEDA = 33

# porção vendida -> (item-mãe que tem o estoque, quantas porções saem de 1)
PORCOES = {
    "84578018": ("84578006", POR_MACO),        # PICADO PORTO FARIA       -> MAÇO PORTO FARIA
    "84761036": ("84761083", POR_MACO),        # PICADO PORTO FARIA MENTA -> MAÇO PORTO FARIA MENTA
    "84578017": ("84578005", POR_MACO),        # PICADO MANDELLE          -> MAÇO MANDELLE
    "84578015": ("84578003", POR_MACO),        # PICADO COYOTE            -> MAÇO COYOTE
    "84578019": ("84578008", POR_MACO),        # PICADO SAN MARINO        -> MAÇO SAN MARINO
    "95571635": ("88750440", POR_MACO),        # PICADO COELHO            -> MAÇO COELHO
    "84578020": ("84578009", POR_CAIXA_SEDA),  # PICADO SEDA ZOMO         -> CAIXA SEDA ZOMO PRETA
    "88995258": ("88750632", POR_CAIXA_SEDA),  # PICADO SEDA ZOMO ALFALFA -> CAIXA SEDA ZOMO ALFALFA
}

# cigarro avulso: o SKU é só o preço, não diz a marca — rateado entre os maços de filtro
AVULSO = ["84578016", "95428387", "95428388", "95428389"]
MACOS_CIGARRO = ["84734030", "84578007", "88747811",              # Rothmans
                 "84734033", "84734032", "84734027", "88673864",  # Lucky
                 "84734028", "84578004", "88748684", "88748569",  # Dunhill
                 "84734275", "84734274", "88748177",              # Marlboro
                 "84734271", "85750322",                          # Chesterfield
                 "88673539",                                      # Kent
                 "95534346", "95534347"]                          # Djarum

CONTROLE_NOME = "CONTROLE BAIXA TABACARIA"


# ---------- estado (mora no GestãoClick, não em disco) ----------

def _acha_controle(gc):
    for p in gc.get_all("/produtos", {}):
        if (p.get("nome") or "").upper() == CONTROLE_NOME:
            return str(p.get("id"))
    r = gc.post("/produtos", {"nome": CONTROLE_NOME, "movimenta_estoque": "0",
                              "ativo": "0", "valor_custo": "0.00", "valor_venda": "0.00",
                              "descricao": "{}"})
    return str((r.get("data") or {}).get("dados") or (r.get("data") or {}).get("id"))


def carrega_estado(gc, controle_id=None):
    """{'ate': 'AAAA-MM-DD HH:MM:SS', 'saldo': {maco_id: porções que sobraram}}"""
    pid = controle_id or _acha_controle(gc)
    d = gc.get(f"/produtos/{pid}").get("data") or {}
    try:
        e = json.loads(d.get("descricao") or "{}")
    except ValueError:
        e = {}
    if not isinstance(e, dict):
        e = {}
    e.setdefault("ate", "")
    e.setdefault("saldo", {})
    return e, pid


def salva_estado(gc, estado, controle_id):
    d = gc.get(f"/produtos/{controle_id}").get("data") or {}
    estado = {"ate": estado.get("ate") or "",
              "saldo": {k: round(float(v), 2) for k, v in (estado.get("saldo") or {}).items()
                        if float(v) > 0}}
    gc.put(f"/produtos/{controle_id}", {"nome": d.get("nome"),
                                        "codigo_interno": d.get("codigo_interno"),
                                        "descricao": json.dumps(estado, ensure_ascii=False)})
    return estado


# ---------- cálculo ----------

def vendas_novas(gc, ate, dias=10, hoje=None):
    """Vendas (balcão + delivery) cadastradas DEPOIS do último corte.

    O delivery entra aqui: o app também vende cigarro avulso, e a versão antiga que
    rodava na mão só olhava o balcão.
    """
    fim = hoje or datetime.date.today()
    ini = fim - datetime.timedelta(days=dias)
    out = []
    for tipo in ("vendas_balcao", "produto"):
        for v in gc.get_all("/vendas", {"tipo": tipo, "data_inicio": ini.isoformat(),
                                        "data_fim": fim.isoformat()}):
            if "cancel" in (v.get("nome_situacao") or "").lower():
                continue
            if tipo == "produto" and "ANOTA AI" not in (v.get("observacoes") or "").upper():
                continue          # saque e afins não são venda de tabacaria
            if ate and (v.get("cadastrado_em") or "") <= ate:
                continue          # já contabilizada numa rodada anterior
            out.append(v)
    return out


def peso_compras(gc, desde):
    """Proporção de cada marca de cigarro nas compras (fallback: partes iguais)."""
    w = defaultdict(float)
    for c in gc.get_all("/compras", {"data_inicio": desde,
                                     "data_fim": datetime.date.today().isoformat()}):
        quando = (c.get("data_emissao") or c.get("data") or c.get("cadastrado_em") or "")[:10]
        if quando and quando < desde:
            continue
        for it in (c.get("produtos") or []):
            p = it.get("produto", it)
            pid = str(p.get("produto_id") or "")
            if pid in MACOS_CIGARRO:
                w[pid] += float(p.get("quantidade") or 0) * float(p.get("valor_custo") or 0)
    tot = sum(w.values())
    if tot <= 0:
        return {pid: 1.0 / len(MACOS_CIGARRO) for pid in MACOS_CIGARRO}
    return {pid: v / tot for pid, v in w.items()}


def calcula(vendas, saldo, pesos):
    """Devolve (baixas_por_maço, novo_saldo, porções_vendidas, detalhe).

    saldo e novo_saldo são em PORÇÕES paradas por maço; a baixa é sempre em maço
    inteiro, igual à garrafa do whisky.
    """
    porcoes = defaultdict(float)        # maço -> porções que ele deve
    vendidas = avulsos = 0.0
    for v in vendas:
        for w in (v.get("produtos") or []):
            p = w.get("produto", w)
            pid = str(p.get("produto_id") or "")
            n = float(p.get("quantidade") or 0)
            if not n:
                continue
            if pid in PORCOES:
                mae, _rende = PORCOES[pid]
                porcoes[mae] += n
                vendidas += n
            elif pid in AVULSO:
                avulsos += n
                vendidas += n
    if avulsos:
        for maco, peso in (pesos or {}).items():
            if peso:
                porcoes[maco] += avulsos * peso

    rende_de = {mae: rende for mae, rende in PORCOES.values()}
    novo_saldo = {k: float(v) for k, v in (saldo or {}).items()}
    baixas, detalhe = {}, {}
    for maco, n in porcoes.items():
        rende = rende_de.get(maco, POR_MACO)
        total = novo_saldo.get(maco, 0.0) + n
        cheios = int(total // rende)
        novo_saldo[maco] = round(total - cheios * rende, 4)
        detalhe[maco] = {"porcoes": n, "sobra": novo_saldo[maco], "por_maco": rende}
        if cheios:
            baixas[maco] = float(cheios)
    return baixas, {k: v for k, v in novo_saldo.items() if v > 0}, vendidas, detalhe


def aplica(gc, baixas):
    """Desconta do estoque. Devolve as linhas aplicadas (antes/depois)."""
    linhas = []
    for pid, qtd in sorted(baixas.items(), key=lambda kv: -kv[1]):
        if qtd < 1:
            continue
        d = gc.get(f"/produtos/{pid}").get("data")
        d = d[0] if isinstance(d, list) else d
        d = d.get("Produto", d) if isinstance(d, dict) else {}
        atual = float(d.get("estoque") or 0)
        novo = round(atual - qtd, 2)
        gc.put(f"/produtos/{pid}", {"nome": d.get("nome"),
                                    "codigo_interno": d.get("codigo_interno"),
                                    "estoque": str(novo)})
        linhas.append({"produto_id": pid, "nome": d.get("nome"),
                       "baixado": round(qtd, 2), "antes": atual, "depois": novo})
    return linhas


def rodar(gc, aplicar=False, dias=10, hoje=None):
    """Uma rodada completa. Só grava (estoque + estado) quando aplicar=True."""
    estado, controle_id = carrega_estado(gc)
    vendas = vendas_novas(gc, estado.get("ate"), dias=dias, hoje=hoje)
    desde = ((hoje or datetime.date.today()) - datetime.timedelta(days=90)).isoformat()
    pesos = peso_compras(gc, desde) if vendas else {}
    baixas, novo_saldo, vendidas, detalhe = calcula(vendas, estado.get("saldo"), pesos)

    # o novo corte é a venda mais recente que acabou de entrar na conta
    maior = max((v.get("cadastrado_em") or "" for v in vendas), default="")
    linhas = aplica(gc, baixas) if aplicar else []
    if aplicar:
        salva_estado(gc, {"ate": maior or estado.get("ate"), "saldo": novo_saldo},
                     controle_id)

    return {"vendas_novas": len(vendas), "porcoes": vendidas, "aplicado": aplicar,
            "linhas": linhas, "baixas": baixas, "saldo": novo_saldo,
            "ate": maior or estado.get("ate"), "detalhe": detalhe}


def saldo_legivel(gc, saldo):
    """O saldo em formato de tela: nome do maço, porções paradas e o que fecha um."""
    rende_de = {mae: rende for mae, rende in PORCOES.values()}
    nomes = {}
    for p in gc.get_all("/produtos", {}):
        pid = str(p.get("id"))
        if pid in saldo:
            nomes[pid] = p.get("nome")
    return [{"maco": nomes.get(k, k), "porcoes": round(float(v), 1),
             "por_maco": rende_de.get(k, POR_MACO)}
            for k, v in sorted(saldo.items(), key=lambda kv: -float(kv[1]))]

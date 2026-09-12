#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Varredura das NFC-e do delivery — versão da NUVEM (antes era a rotina das 9h no Mac).

O que faz: procura venda de delivery (observação "Anota AI") dos últimos dias que ficou
SEM nota fiscal e resolve. Tenta emitir de verdade; se a API do GestãoClick recusar,
deixa o rascunho pronto pro dono só clicar Emitir no painel do GC.

Por que existe mesmo com o robô do delivery já emitindo na importação (anota.py): se a
emissão falha naquele instante — API fora do ar, rejeição da SEFAZ, serviço reiniciando
no meio — a venda fica sem nota e ninguém percebe. Esta varredura é a rede de segurança
que roda todo dia e pega o que sobrou.

ONDE FICA O CONTROLE: num produto técnico inativo do próprio GestãoClick
("CONTROLE NFCE DELIVERY"), no campo descrição — mesmo truque da baixa dos drinks. Não
pode ser arquivo em disco: na nuvem o disco some a cada reinício, e o GC NÃO grava o
pedido_id no RASCUNHO (só quando a nota é emitida). Sem esse registro, cada rodada
criaria nota nova pra mesma venda — em 07/09/2026 isso virou 153 rascunhos para 66
vendas.
"""
import json, re, time
from datetime import date, datetime, timedelta

GC_LOJA = "529233"
CFOP_VENDA_BALCAO = "289"          # CFOP 5102, igual ao PDV
FORMA_PADRAO = "6055931"           # PIX (fallback quando a venda não tem pagamento)
CONTROLE_NOME = "CONTROLE NFCE DELIVERY"

# O GestãoClick APAGA o pedido_id quando a nota é emitida pela API (só as notas feitas
# no PDV guardam). Sem vínculo, os dois robôs emitiam a mesma nota duas vezes — foram 41
# notas repetidas entre 01/08 e 12/09/2026. A marca abaixo vai em "informações
# complementares", que é campo nosso, sobrevive à emissão e volta na consulta.
MARCA = "Pedido "
RE_MARCA = re.compile(r"Pedido\s+(\d{6,})")

# venda recém-lançada pode estar sendo faturada pelo robô do delivery neste exato
# instante — esperar um pouco evita os dois criarem nota pra mesma venda.
CARENCIA_MIN = 20


def num(x):
    try:
        return float(str(x).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- controle no GC
def _acha_controle(gc):
    for p in gc.get_all("/produtos", {"nome": CONTROLE_NOME}):
        if (p.get("nome") or "").upper() == CONTROLE_NOME:
            return str(p.get("id"))
    r = gc.post("/produtos", {"nome": CONTROLE_NOME, "movimenta_estoque": "0",
                              "ativo": "0", "valor_custo": "0.00", "valor_venda": "0.00",
                              "descricao": "{}"})
    return str((r.get("data") or {}).get("dados") or (r.get("data") or {}).get("id"))


def carrega_estado(gc, controle_id=None):
    """{'rascunhos': {venda_id: nota_id}, 'quando': '...'}"""
    pid = controle_id or _acha_controle(gc)
    d = gc.get(f"/produtos/{pid}").get("data") or {}
    try:
        e = json.loads(d.get("descricao") or "{}")
    except ValueError:
        e = {}
    if not isinstance(e, dict):
        e = {}
    e.setdefault("rascunhos", {})
    e.setdefault("quando", "")
    e["_id"] = pid
    e["_nome"] = d.get("nome") or CONTROLE_NOME
    return e


def salva_estado(gc, estado):
    """PUT no GestãoClick quer o corpo inteiro — mandar só a descrição zera o resto."""
    pid = estado.get("_id")
    corpo = {k: v for k, v in estado.items() if not k.startswith("_")}
    gc.put(f"/produtos/{pid}", {"nome": estado.get("_nome") or CONTROLE_NOME,
                                "movimenta_estoque": "0", "ativo": "0",
                                "valor_custo": "0.00", "valor_venda": "0.00",
                                "descricao": json.dumps(corpo, ensure_ascii=False)})


# ---------------------------------------------------------------- notas existentes
def notas_do_periodo(gc, ini, fim, rascunhos):
    """venda_id -> situação da nota, juntando TRÊS pistas, da mais forte pra mais fraca:
    a marca "Pedido N" que a gente escreve na nota, o pedido_id (só sobrevive nas notas
    do PDV) e o registro de rascunhos guardado no GestãoClick.
    Devolve também quais ids do registro sumiram do GC, pra limpar."""
    por_venda, vivas = {}, {}
    d0 = (date.fromisoformat(ini) - timedelta(days=3)).isoformat()
    d1 = (date.fromisoformat(fim) + timedelta(days=8)).isoformat()
    for n in gc.get_all("/notas_fiscais_consumidores", {"data_inicio": d0, "data_fim": d1}):
        sit = n.get("situacao_nf") or "?"
        vivas[str(n.get("id"))] = sit
        m = RE_MARCA.search(n.get("informacoes_complementares") or "")
        if m:
            por_venda[m.group(1)] = sit
        pid = str(n.get("pedido_id") or "")
        if pid and pid not in por_venda:
            por_venda[pid] = sit
    mortos = []
    for venda_id, nid in (rascunhos or {}).items():
        sit = vivas.get(str(nid))
        if sit is None:
            mortos.append(venda_id)          # rascunho apagado no GC: pode recriar
        elif venda_id not in por_venda:
            por_venda[venda_id] = sit
    return por_venda, mortos


# ---------------------------------------------------------------- montar / emitir
def monta_rascunho(gc, v):
    """Cria a nota da venda no GC e devolve o id do rascunho (ou None)."""
    prods, orfaos = [], []
    for p in v.get("produtos", []):
        q = p.get("produto", p)
        if not str(q.get("produto_id") or "").strip():
            # item que o app vendeu sem produto cadastrado no GestãoClick. O GC
            # SILENCIOSAMENTE joga essa linha fora e a nota sai com valor MENOR que a
            # venda — foi o que aconteceu com a NF 12344 (R$ 24,00 numa venda de
            # R$ 28,50, faltando o Cebolitos). Melhor não emitir nota nenhuma e avisar.
            orfaos.append((q.get("detalhes") or q.get("nome_produto") or "item sem nome"))
            continue
        prods.append({"produto_id": q["produto_id"],
                      "quantidade": float(q["quantidade"]),
                      # unitário já arredondado em 2 casas: é assim que o GC grava na
                      # nota. Com o valor cheio o total do pagamento nasce diferente do
                      # total da nota e a correção por PUT reprova na SEFAZ
                      # ("Rejeição 899: meio de pagamento").
                      "valor_venda": round(float(q["valor_venda"]), 2)})
    if orfaos:
        return None, ("item sem produto cadastrado no sistema, a nota sairia com valor "
                      "menor que a venda: " + "; ".join(orfaos[:3]))
    if not prods:
        return None, "venda sem produtos"

    pg = (v.get("pagamentos") or [{}])[0]
    forma = (pg.get("pagamento", pg) or {}).get("forma_pagamento_id") or FORMA_PADRAO
    dia = (v.get("data") or "")[:10]
    dt = f"{dia[8:10]}/{dia[5:7]}/{dia[:4]}"
    total = round(sum(p["quantidade"] * p["valor_venda"] for p in prods), 2)
    body = {"loja_id": GC_LOJA, "pedido_id": str(v.get("id")), "tipo_atendimento": 1,
            "tipo_nf": "1", "consumidor_final": 1, "natureza_operacao": "Venda balcão",
            "cfop_id": CFOP_VENDA_BALCAO, "produtos": prods,
            "informacoes_complementares": MARCA + str(v.get("id")),
            "pagamento": [{"forma_pagamento_id": forma, "valor_pagamento": total,
                           "data_vencimento": dt}]}
    r = gc.post("/notas_fiscais_consumidores", body)
    nid = (r.get("data") or {}).get("dados")
    if not nid:
        return None, f"GC recusou: {json.dumps(r, ensure_ascii=False)[:120]}"

    # se o total da nota sair diferente do pagamento, refaz (NUNCA consertar por PUT)
    n = gc.get(f"/notas_fiscais_consumidores/{nid}").get("data") or {}
    tot_nota = round(sum(num(p.get("valor_venda")) for p in n.get("produtos", [])), 2)
    if abs(tot_nota - total) > 0.001:
        try:
            gc.delete(f"/notas_fiscais_consumidores/{nid}")
        except Exception:
            pass
        body["pagamento"][0]["valor_pagamento"] = tot_nota
        r = gc.post("/notas_fiscais_consumidores", body)
        nid = (r.get("data") or {}).get("dados")
        if not nid:
            return None, "não bateu o total nem na segunda tentativa"
    return nid, None


def tenta_emitir(gc, nid):
    """Transmite pra SEFAZ. Voltou a funcionar em 12/09/2026 (ficou 404 de 02 a 11/09)."""
    try:
        e = gc.post(f"/notas_fiscais_consumidores/emitir/{nid}", {})
    except Exception as err:
        return None, str(err)[:80]
    if (e.get("data") or {}).get("ok"):
        n = gc.get(f"/notas_fiscais_consumidores/{nid}").get("data") or {}
        return n.get("numero_nf") or nid, None
    return None, json.dumps(e, ensure_ascii=False)[:80]


# ---------------------------------------------------------------- varredura
def rodar(gc, dias=7, ini=None, fim=None):
    """Sem argumento: varre os últimos 7 dias. Janela larga de propósito: se a nuvem
    ficar fora do ar alguns dias, a volta recupera tudo sozinha."""
    hoje = date.today()
    fim = fim or hoje.isoformat()
    ini = ini or (hoje - timedelta(days=max(dias, 1) - 1)).isoformat()

    vendas = [v for v in gc.get_all("/vendas", {"tipo": "produto",
                                                "data_inicio": ini, "data_fim": fim})
              if "ANOTA AI" in (v.get("observacoes") or "").upper()
              and "cancel" not in (v.get("nome_situacao") or "").lower()]

    estado = carrega_estado(gc)
    rasc = dict(estado.get("rascunhos") or {})
    ja, mortos = notas_do_periodo(gc, ini, fim, rasc)
    for m in mortos:                       # rascunho que não existe mais no GC
        rasc.pop(m, None)

    limite = datetime.now() - timedelta(minutes=CARENCIA_MIN)
    emitidas, esperando, erros, novas = [], [], [], 0
    for v in sorted(vendas, key=lambda x: (x.get("data") or "", str(x.get("id")))):
        vid = str(v.get("id"))
        sit = ja.get(vid)
        if sit and sit != "Em aberto":
            continue                       # já tem nota emitida
        if sit == "Em aberto":
            esperando.append({"venda": vid, "nota": rasc.get(vid), "valor": v.get("valor_total"),
                              "data": (v.get("data") or "")[:10]})
            continue                       # rascunho já existe, espera o clique
        cad = (v.get("cadastrado_em") or "")[:19]
        if cad:
            try:
                if datetime.strptime(cad, "%Y-%m-%d %H:%M:%S") > limite:
                    continue               # o robô do delivery ainda pode estar faturando
            except ValueError:
                pass

        nid, err = monta_rascunho(gc, v)
        if not nid:
            erros.append({"venda": vid, "erro": err, "data": (v.get("data") or "")[:10]})
            continue
        rasc[vid] = str(nid)
        salva_estado(gc, dict(estado, rascunhos=rasc))   # grava ANTES de emitir
        novas += 1
        nf, _ = tenta_emitir(gc, nid)
        if nf:
            emitidas.append({"venda": vid, "nf": nf, "valor": v.get("valor_total"),
                             "data": (v.get("data") or "")[:10]})
            rasc.pop(vid, None)            # emitida: o pedido_id agora está na nota
        else:
            esperando.append({"venda": vid, "nota": str(nid), "valor": v.get("valor_total"),
                              "data": (v.get("data") or "")[:10]})
        time.sleep(0.4)

    # poda: o registro só precisa cobrir a janela que a varredura olha. Sem isso ele
    # cresce pra sempre e não cabe mais no campo descrição do produto de controle.
    da_janela = {str(v.get("id")) for v in vendas}
    rasc = {k: v2 for k, v2 in rasc.items() if k in da_janela}
    estado["rascunhos"] = rasc
    estado["quando"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    salva_estado(gc, estado)

    return {"ok": True, "de": ini, "ate": fim, "vendas": len(vendas),
            "emitidas": emitidas, "esperando": esperando, "erros": erros,
            "quando": datetime.now().strftime("%d/%m/%Y %H:%M")}

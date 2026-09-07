#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Baixa no estoque a GARRAFA que virou copão / dose / combo.

Mora aqui, dentro do webapp, de propósito: o MESMO código roda no Mac
(scripts/baixa_drinks.py) e na NUVEM (app.py), sem duas versões pra divergir.

Copão, dose e combo não são produtos de prateleira (movimenta_estoque=0) — são
montados na hora. Quem tem estoque de verdade é a GARRAFA.

REGRA DA CASA (definida pelo dono em 07/09/2026): a garrafa só sai do estoque
INTEIRA, quando os copos vendidos fecham uma garrafa:

    1 L e 900 ml   -> a cada 10 copos, baixa 1 garrafa
    700 e 750 ml   -> a cada  8 copos, baixa 1 garrafa

O que sobra fica de SALDO e entra na próxima rodada. Antes o script descontava
fração (0,09 garrafa por dose) e o estoque nunca fechava garrafa inteira.

ONDE FICA O CONTROLE: num produto técnico inativo do próprio GestãoClick
("CONTROLE BAIXA DRINKS"), no campo descrição. Não pode ser arquivo em disco: na
nuvem o disco é apagado a cada reinício e, sem o registro, a rotina reprocessaria
vendas já baixadas e descontaria o estoque EM DOBRO. O corte é o `cadastrado_em`
da venda (traz hora), então venda lançada com data retroativa também entra.
"""
import json, os

DOSE_ML = 90
GELO = "84577979"        # GELO SABORIZADOS DRINKS — 1 por copão, sai inteiro
RED_BULL = "84577970"    # RED BULL LT 250 ML SABORES
RB_POR_COMBO = 5
# ENERGY JACK POWER 2L: cada copão leva 400 ml, então a garrafa de 2 L dá 5 copões.
# Antes descontava 0,21 de garrafa por copão e o estoque ficava quebrado (58,1); agora
# fecha garrafa inteira igual às de destilado, com o resto guardando pra próxima.
JACK_POWER = "84577972"
JP_ML_POR_COPAO = 400
JP_VOLUME_ML = 2000
JP_POR_GARRAFA = JP_VOLUME_ML // JP_ML_POR_COPAO      # 5 copões por garrafa

CONTROLE_NOME = "CONTROLE BAIXA DRINKS"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAPA_PATH = os.path.join(BASE_DIR, "mapa_drinks.json")


def carrega_mapa():
    with open(MAPA_PATH, encoding="utf-8") as f:
        return json.load(f)


def copos_por_garrafa(volume_ml):
    """Quantos copos a casa tira de uma garrafa. É a regra do dono, NÃO volume÷90:
    o resto da garrafa vira perda/sobra de serviço."""
    v = int(volume_ml or 1000)
    if v >= 900:
        return 10          # 1 L e 900 ml
    if v >= 700:
        return 8           # 700 e 750 ml
    if v >= 600:
        return 6
    return max(1, v // DOSE_ML)


def limite_de(produto_id, volume_ml):
    """Quantos copões fecham uma unidade deste produto."""
    if str(produto_id) == JACK_POWER:
        return JP_POR_GARRAFA          # 2 L ÷ 400 ml = 5 copões
    return copos_por_garrafa(volume_ml)


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
    """{'ate': 'AAAA-MM-DD HH:MM:SS', 'saldo': {garrafa_id: copos}}"""
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
    """Vendas (balcão + delivery) cadastradas DEPOIS do último corte."""
    import datetime
    fim = hoje or datetime.date.today()
    ini = fim - datetime.timedelta(days=dias)
    out = []
    for tipo in ("vendas_balcao", "produto"):
        for v in gc.get_all("/vendas", {"tipo": tipo, "data_inicio": ini.isoformat(),
                                        "data_fim": fim.isoformat()}):
            if "cancel" in (v.get("nome_situacao") or "").lower():
                continue
            if tipo == "produto" and "ANOTA AI" not in (v.get("observacoes") or "").upper():
                continue          # saque e afins não são venda de drink
            if ate and (v.get("cadastrado_em") or "") <= ate:
                continue          # já contabilizada numa rodada anterior
            out.append(v)
    return out


def calcula(vendas, mapa, saldo):
    """Devolve (baixas_por_produto, novo_saldo, n_itens, detalhe_por_garrafa)."""
    from collections import defaultdict
    copos = defaultdict(float)
    inteiras = defaultdict(float)
    extras = defaultdict(float)
    n_itens = 0.0
    for v in vendas:
        for w in (v.get("produtos") or []):
            p = w.get("produto", w)
            info = mapa.get(str(p.get("produto_id") or ""))
            if not info:
                continue
            n = float(p.get("quantidade") or 0)
            if not n:
                continue
            n_itens += n
            nome = info["nome"].upper()
            garrafa = info["garrafa"]
            if nome.startswith("COMBO"):
                inteiras[garrafa] += n                 # combo leva a garrafa fechada
                if "RED BULL" in nome:
                    extras[RED_BULL] += n * RB_POR_COMBO
            else:
                # DOSE e COPÃO contam igual: os dois tiram 90 ml da garrafa. Quem tem
                # dose diferente (a Vila Ouro é de 80 ml) entra proporcional, senão a
                # garrafa fechava cedo demais — 1 L rende 12 doses de 80, não 10.
                dose = float(info.get("dose_ml") or DOSE_ML)
                copos[garrafa] += n * (dose / DOSE_ML)
                if nome.startswith("COPAO") or nome.startswith("COPÃO"):
                    extras[GELO] += n           # 1 gelinho por copão, inteiro
                    copos[JACK_POWER] += n      # 400 ml: fecha 1 garrafa a cada 5 copões

    volume_de = {i["garrafa"]: (i.get("volume") or 1000) for i in mapa.values()}
    novo_saldo = {k: float(v) for k, v in (saldo or {}).items()}
    baixas = defaultdict(float)
    detalhe = {}
    for garrafa, n in copos.items():
        total = novo_saldo.get(garrafa, 0.0) + n
        por = limite_de(garrafa, volume_de.get(garrafa, 1000))
        cheias = int(total // por)
        novo_saldo[garrafa] = total - cheias * por
        detalhe[garrafa] = {"copos": n, "sobra": novo_saldo[garrafa], "por_garrafa": por}
        if cheias:
            baixas[garrafa] += cheias
    for garrafa, n in inteiras.items():
        baixas[garrafa] += n
    for pid, n in extras.items():
        baixas[pid] += n
    return dict(baixas), {k: v for k, v in novo_saldo.items() if v > 0}, n_itens, detalhe


def aplica(gc, baixas):
    """Desconta do estoque. Devolve as linhas aplicadas (antes/depois)."""
    linhas = []
    for pid, qtd in sorted(baixas.items(), key=lambda kv: -kv[1]):
        if qtd < 0.01:
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
    mapa = carrega_mapa()
    estado, controle_id = carrega_estado(gc)
    vendas = vendas_novas(gc, estado.get("ate"), dias=dias, hoje=hoje)
    baixas, novo_saldo, n_itens, detalhe = calcula(vendas, mapa, estado.get("saldo"))

    # o novo corte é a venda mais recente que acabou de entrar na conta
    maior = max((v.get("cadastrado_em") or "" for v in vendas), default="")
    linhas = aplica(gc, baixas) if aplicar else []
    if aplicar:
        salva_estado(gc, {"ate": maior or estado.get("ate"), "saldo": novo_saldo}, controle_id)

    return {"vendas_novas": len(vendas), "itens": n_itens, "aplicado": aplicar,
            "linhas": linhas, "baixas": baixas,
            "saldo": saldo_legivel(mapa, novo_saldo),
            "ate": maior or estado.get("ate"), "detalhe": detalhe}


def saldo_legivel(mapa, saldo):
    """O saldo em formato de tela: nome, copos parados e quanto fecha a unidade."""
    nomes = {i["garrafa"]: i.get("garrafa_nome") or i["garrafa"] for i in mapa.values()}
    nomes.setdefault(JACK_POWER, "ENERGY JACK POWER 2L (400 ml por copão)")
    vols = {i["garrafa"]: (i.get("volume") or 1000) for i in mapa.values()}
    return [{"garrafa": nomes.get(k, k), "copos": round(float(v), 1),
             "por_garrafa": limite_de(k, vols.get(k, 1000))}
            for k, v in sorted(saldo.items(), key=lambda kv: -float(kv[1]))]

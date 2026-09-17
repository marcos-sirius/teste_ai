"""
Índice de conteúdo REAPROVEITÁVEL entre estudos diferentes (ex: Português de
"Analista de Dados" pode servir pra Português de "Auditor TCE").

Fica em UM ÚNICO arquivo (banco_compartilhado.json) na RAIZ da pasta do
Drive. Cada entrada representa "esse eixo/tema, desse estudo, tem questões
já geradas que podem ser emprestadas pra outro estudo compatível" — só
METADADOS aqui (não duplica o texto das questões); na hora de reaproveitar
de verdade, lemos as questões direto do Consolidado do estudo de origem
(excel_manager.questoes_por_tema).

Salvaguardas:
  - Só empresta entre estudos do MESMO NÍVEL (ex: não mistura Médio com
    Superior — o cuidado que você pediu).
  - Só considera lotes gerados dentro de uma janela de tempo configurável
    (1 a 12 meses), pra não reaproveitar conteúdo defasado.
  - Cada lote só pode ser emprestado um número limitado de vezes
    (MAX_EMPRESTIMOS_POR_LOTE) — depois disso, "zerou" e passa a gerar
    conteúdo novo em vez de reciclar pra sempre.
"""
import datetime as dt
import json
import uuid

import drive_storage as ds

NOME_ARQUIVO = "banco_compartilhado.json"
MAX_EMPRESTIMOS_POR_LOTE = 3


def carregar_indice() -> list:
    arquivo = ds.buscar_arquivo(NOME_ARQUIVO, ds.root_folder_id())
    if not arquivo:
        return []
    dados = ds.baixar_bytes(arquivo["id"])
    return json.loads(dados.decode("utf-8")).get("lotes", [])


def salvar_indice(lotes: list):
    arquivo = ds.buscar_arquivo(NOME_ARQUIVO, ds.root_folder_id())
    dados = json.dumps({"lotes": lotes}, ensure_ascii=False, indent=2).encode("utf-8")
    ds.salvar_bytes(NOME_ARQUIVO, dados, ds.root_folder_id(), "application/json",
                     file_id=arquivo["id"] if arquivo else None)


def registrar_novo_lote(estudo_folder_id: str, estudo_nome: str, banca: str, nivel: str,
                         eixo: str, tema: str, qtd_questoes: int):
    """Chamado toda vez que um tema é gerado de verdade (não reaproveitado) — vira candidato pro futuro."""
    if qtd_questoes <= 0:
        return
    lotes = carregar_indice()
    lotes.append({
        "id": str(uuid.uuid4()),
        "estudo_folder_id": estudo_folder_id,
        "estudo_nome": estudo_nome,
        "banca": banca,
        "nivel": nivel,
        "eixo": eixo,
        "tema": tema,
        "gerado_em": dt.datetime.now().isoformat(timespec="seconds"),
        "qtd_questoes": qtd_questoes,
        "usos_emprestados": 0,
    })
    salvar_indice(lotes)


def buscar_candidatos(banca: str, nivel: str, eixo: str, meses_janela: int, excluir_estudo_folder_id: str) -> list:
    """
    Lotes de OUTROS estudos, MESMA BANCA e MESMO NÍVEL, mesmo eixo (nome
    idêntico), dentro da janela de meses, e que ainda não bateram o limite
    de empréstimos. Banca e nível juntos: uma questão de Analista Superior
    da FGV não deve ser emprestada pra um estudo de Técnico da CESPE, por
    exemplo, mesmo que o eixo/tema pareçam parecidos.
    """
    limite_data = dt.datetime.now() - dt.timedelta(days=meses_janela * 30)
    candidatos = []
    for lote in carregar_indice():
        if lote["estudo_folder_id"] == excluir_estudo_folder_id:
            continue
        if lote.get("banca") != banca:
            continue
        if lote["nivel"] != nivel:
            continue
        if lote["eixo"] != eixo:
            continue
        if lote.get("usos_emprestados", 0) >= MAX_EMPRESTIMOS_POR_LOTE:
            continue
        try:
            gerado_em = dt.datetime.fromisoformat(lote["gerado_em"])
        except ValueError:
            continue
        if gerado_em < limite_data:
            continue
        candidatos.append(lote)
    return candidatos


def incrementar_uso(lote_id: str):
    lotes = carregar_indice()
    for lote in lotes:
        if lote["id"] == lote_id:
            lote["usos_emprestados"] = lote.get("usos_emprestados", 0) + 1
            break
    salvar_indice(lotes)

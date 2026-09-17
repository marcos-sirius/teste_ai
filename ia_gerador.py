"""
Geração de questões via API da OpenAI, usando a Responses API com Structured
Outputs (schema strict) e reasoning effort configurável.

Fluxos implementados:
1. Geração (GPT) com exigência de duplo-fator no gabarito (índice + letra).
2. Sanitização (Python) limpando A), B), C) das alternativas via Regex.
3. Validação Lógica (Python) confirmando se o índice bate com a letra defendida.
4. Loop de retentativa: se uma questão sair inconsistente, gera substituta
   até bater a meta (tudo em Python, sem chamada extra de auditoria por IA).
5. Balanceamento e persistência mantidos.

OBS: a versão anterior deste arquivo tinha uma etapa de "auditoria" com uma
chamada extra de IA (reasoning_effort="high") por QUESTÃO, além da chamada
de geração. Isso multiplicava o custo por simulado em 6-17x (medido: de
~$0,02-0,21 para ~$1,24-3,49 por simulado de 70 questões), porque cada
auditoria gastava tokens de raciocínio invisíveis cobrados como output.
Removida por ora — o gpt-5.6-terra já vem saindo tecnicamente sólido sem
essa camada extra, e a validação Python (abaixo) já pega os erros estruturais
mais comuns (gabarito inconsistente, menos/mais de 5 alternativas, etc.)
sem gastar nenhum token adicional.
"""

import json
import os
import random
import time
import re

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError

# ============================================================
# CONFIGURAÇÃO
# ============================================================

load_dotenv()

MODEL = "gpt-5.6-terra"

# Modelo separado, mais barato, só pra decisões simples de sim/não (ex:
# "esses dois temas são parecidos o suficiente?") — não vale gastar o modelo
# de geração de conteúdo pra uma pergunta binária.
MODEL_COMPARACAO = "gpt-5.6-luna"
MAX_TENTATIVAS = 5  # quantas vezes tenta regenerar questões rejeitadas por Python

api_key = os.environ.get("OPENAI_API_KEY")

if not api_key:
    raise RuntimeError(
        "OPENAI_API_KEY não encontrada. "
        "Configure a variável OPENAI_API_KEY no ambiente "
        "ou nos Secrets do Streamlit."
    )

client = OpenAI(api_key=api_key)

# ============================================================
# MARCADORES DE LETRA (resolvidos em Python, sem gastar API)
# ============================================================

def marcador_opcao(indice_original: int) -> str:
    """
    Marcador literal que a IA deve escrever no comentário no lugar de QUALQUER
    letra de alternativa (a correta OU as erradas). 'indice_original' é a
    posição (0-4) em que a IA gerou aquela alternativa no array "opcoes" —
    ou seja, é fixo desde a geração, independente de pra onde a alternativa
    for parar depois do embaralhamento em balancear_gabaritos().

    Por que isso existe: a ordem final das alternativas só é decidida DEPOIS
    da questão ser gerada. Se o comentário citasse a letra de verdade (ex:
    "a alternativa C está correta, diferente de A e E"), esse texto ficaria
    desatualizado assim que a posição mudasse. Com o marcador, o Python troca
    cada um pela letra final certa em _embaralhar_questao(), num simples
    replace() — sem gastar nenhuma chamada extra de API.
    """
    return f"[[LETRA_OPCAO_{indice_original + 1}]]"


MARCADORES_TODAS_OPCOES = [marcador_opcao(i) for i in range(5)]

# ============================================================
# SCHEMAS
# ============================================================

SCHEMA_QUESTOES = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "questoes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "pergunta": {"type": "string"},
                    "opcoes": {
                        "type": "array",
                        "items": {"type": "string"},
                        # OBS: "minItems"/"maxItems" foram removidos daqui de
                        # propósito. O modo strict:true da OpenAI NÃO suporta
                        # essas palavras-chave — incluí-las faz a API rejeitar
                        # a chamada inteira com erro 400 "Invalid schema" em
                        # TODAS as tentativas, sem exceção (foi exatamente o
                        # bug que causava "Falhou ao gerar" em tudo). A
                        # validação de "exatamente 5 alternativas" já é feita
                        # em Python por _validar_estrutura_questao(), então
                        # nada de segurança se perde.
                    },
                    "correta": {
                        "type": "integer",
                        # mesma observação: "minimum"/"maximum" também não são
                        # suportados em strict mode. A checagem 0<=correta<=4
                        # já é garantida indiretamente por _gabarito_consistente()
                        # (compara o índice com a letra do enum abaixo).
                    },
                    "letra_gabarito": {
                        "type": "string",
                        "enum": ["A", "B", "C", "D", "E"]
                    },
                    "comentario": {"type": "string"},
                },
                "required": [
                    "pergunta",
                    "opcoes",
                    "correta",
                    "letra_gabarito",
                    "comentario",
                ],
            },
        }
    },
    "required": ["questoes"],
}

# ============================================================
# LÓGICA DE VALIDAÇÃO PYTHON (JUIZ)
# ============================================================

def _gabarito_consistente(correta_idx: int, letra: str) -> bool:
    """Verifica se o índice numérico corresponde à letra declarada."""
    mapa = {0: 'A', 1: 'B', 2: 'C', 3: 'D', 4: 'E'}
    return mapa.get(correta_idx) == letra.upper()

def _limpar_letras_alternativas(texto: str) -> str:
    """Remove A), b-, C. etc do início do texto[cite: 1]."""
    return re.sub(r'^([A-Ea-e][\)\.-]\s*)', '', texto).strip()

# ============================================================
# PROMPT DE GERAÇÃO
# ============================================================

def montar_prompt(banca: str, nivel: str, tema: str, qtd: int, evitar: list = None) -> str:
    bloco_evitar = ""
    if evitar:
        evitar_recentes = evitar[-100:]
        lista = "\n".join(f"- {p}" for p in evitar_recentes)
        bloco_evitar = f"\nNÃO repita nem crie variações óbvias das perguntas abaixo:\n{lista}\n"

    return f"""
Atue como um Examinador de Elite especializado em concursos públicos.
Gere exatamente {qtd} questão(ões) de múltipla escolha.

Banca: {banca}
Nível: {nivel}
Tema: {tema}

REGRAS OBRIGATÓRIAS:
1. Cada questão deve possuir exatamente 5 alternativas.
2. Deve existir exatamente UMA alternativa correta.
3. Não repita questões já utilizadas.
4. Em questões de lógica ou matemática, resolva o problema antes de escolher a resposta.
5. O campo "correta" deve apontar para a alternativa certa: 0=A, 1=B, 2=C, 3=D, 4=E.
6. O campo "letra_gabarito" DEVE ser a letra correspondente ao índice (A, B, C, D ou E).
7. NUNCA escreva uma letra literal (A, B, C, D ou E) no texto do "comentario"
   — nem pra dizer qual está certa, nem pra explicar por que as outras estão
   erradas. Em vez disso, use o marcador correspondente à ORDEM em que você
   gerou cada alternativa no array "opcoes":
     - a 1ª alternativa do array (índice 0) = {MARCADORES_TODAS_OPCOES[0]}
     - a 2ª alternativa do array (índice 1) = {MARCADORES_TODAS_OPCOES[1]}
     - a 3ª alternativa do array (índice 2) = {MARCADORES_TODAS_OPCOES[2]}
     - a 4ª alternativa do array (índice 3) = {MARCADORES_TODAS_OPCOES[3]}
     - a 5ª alternativa do array (índice 4) = {MARCADORES_TODAS_OPCOES[4]}
   Exemplo (supondo que a certa seja a 3ª opção do array e você queira citar
   a 1ª como errada): "a alternativa {MARCADORES_TODAS_OPCOES[2]} está
   correta, ao contrário de {MARCADORES_TODAS_OPCOES[0]}, que afirma...".
   Isso é obrigatório porque a ordem final das alternativas só é decidida
   DEPOIS de gerada a questão (em balancear_gabaritos), e o Python substitui
   cada marcador pela letra final correspondente automaticamente.
8. NÃO escreva a letra da alternativa dentro do texto da opção. Gere apenas o conteúdo da alternativa.

{bloco_evitar}
"""

# ============================================================
# NÚCLEO DE REQUISIÇÃO
# ============================================================

def _responder_json(prompt: str, schema: dict, nome_schema: str, reasoning_effort: str = "medium",
                     modelo: str = MODEL) -> dict:
    response = client.responses.create(
        model=modelo,
        reasoning={"effort": reasoning_effort},
        input=prompt,
        max_output_tokens=8000,
        text={
            "format": {
                "type": "json_schema",
                "name": nome_schema,
                "strict": True,
                "schema": schema,
            }
        },
    )
    texto = response.output_text
    if not texto: raise ValueError("Resposta vazia.")
    try:
        return json.loads(texto)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON inválido: {e}") from e

# ============================================================
# TRATAMENTO E VALIDAÇÃO DA QUESTÃO
# ============================================================

def _validar_estrutura_questao(q: dict) -> None:
    opcoes = q.get("opcoes", [])
    if len(opcoes) != 5:
        raise ValueError("A questão precisa possuir exatamente 5 alternativas.")
    
    normalizadas = [" ".join(opcao.strip().lower().split()) for opcao in opcoes]
    if len(set(normalizadas)) != 5:
        raise ValueError("Existem alternativas repetidas.")

def _normalizar_questao(q: dict) -> dict:
    _validar_estrutura_questao(q)
    
    # 1. Limpeza automática das alternativas (Regex)
    opcoes_limpas = [_limpar_letras_alternativas(op) for op in q["opcoes"]]
    
    return {
        "pergunta": q["pergunta"].strip(),
        "opcoes": opcoes_limpas,
        "correta": int(q["correta"]),
        "letra_gabarito": q["letra_gabarito"].strip().upper(),
        "comentario": q["comentario"].strip(),
    }

# ============================================================
# FLUXO PRINCIPAL COM LOOP DE RETENTATIVA
# ============================================================

def gerar_questoes(banca: str, nivel: str, tema: str, qtd: int, evitar: list = None) -> list:
    if qtd <= 0: return []
    evitar = list(evitar) if evitar else []
    
    aprovadas_finais = []
    tentativa_global = 1

    while len(aprovadas_finais) < qtd and tentativa_global <= MAX_TENTATIVAS:
        faltam = qtd - len(aprovadas_finais)
        print(f"\n[{tema}] Geração {tentativa_global}/{MAX_TENTATIVAS}. Buscando {faltam} questão(ões)...")
        
        try:
            dados = _responder_json(
                prompt=montar_prompt(banca, nivel, tema, faltam, evitar),
                schema=SCHEMA_QUESTOES,
                nome_schema="geracao_questoes",
                reasoning_effort="medium"
            )
            
            questoes_brutas = dados.get("questoes", [])
            
            for q_bruta in questoes_brutas:
                if len(aprovadas_finais) >= qtd: break
                
                try:
                    q_norm = _normalizar_questao(q_bruta)
                    
                    # Validação em Python (sem chamada extra de API): confere
                    # se o índice numérico bate com a letra que o modelo
                    # declarou. Se não bater, descarta e tenta de novo no
                    # próximo loop — mais barato que auditar via IA.
                    if not _gabarito_consistente(q_norm["correta"], q_norm["letra_gabarito"]):
                        print(f"  [GERAÇÃO] ⚠️ Questão descartada (índice/letra incoerentes).")
                        continue

                    print("  [GERAÇÃO] ✓ Questão aprovada.")
                    aprovadas_finais.append(q_norm)
                    evitar.append(q_norm["pergunta"])

                except Exception as e:
                    print(f"  [ERRO ESTRUTURAL] Questão descartada: {e}")
                    
            tentativa_global += 1
            
        except Exception as e:
            print(f"[{tema}] ❌ ERRO de API na tentativa {tentativa_global}: {e}")
            tentativa_global += 1
            time.sleep(5)

    if len(aprovadas_finais) < qtd:
        print(f"[{tema}] ⚠️ Concluído parcialmente: {len(aprovadas_finais)}/{qtd}.")
    else:
        print(f"[{tema}] ✓ Lote concluído com sucesso: {qtd}/{qtd}.")
        
    return aprovadas_finais

# ============================================================
# FUNÇÕES MANTIDAS INTACTAS
# ============================================================

def gerar_bloco(banca: str, nivel: str, tema: str, qtd: int, evitar: list = None, tamanho_bloco: int = 10) -> list:
    """Mesma lógica original de segmentação[cite: 1]."""
    evitar = list(evitar) if evitar else []
    todas_questoes = []
    if qtd <= 0: return todas_questoes
    tamanho_bloco = max(tamanho_bloco, 1)

    blocos = [tamanho_bloco] * (qtd // tamanho_bloco)
    if qtd % tamanho_bloco: blocos.append(qtd % tamanho_bloco)

    for i, qtd_bloco in enumerate(blocos, 1):
        sufixo_tema = f"{tema} (Bloco {i}/{len(blocos)})" if len(blocos) > 1 else tema
        print(f"\n[{tema}] Iniciando bloco {i}/{len(blocos)} ({qtd_bloco} q)...")
        questoes = gerar_questoes(banca, nivel, sufixo_tema, qtd_bloco, evitar)
        todas_questoes.extend(questoes)
        evitar.extend(q.get("pergunta", "") for q in questoes if q.get("pergunta"))

    return todas_questoes

def _comentario_cita_letra(comentario: str) -> bool:
    """
    Detecta se o texto do comentário ainda menciona uma letra de alternativa
    (ex: "alternativa C", "opção B") — sinal de que a IA não seguiu a regra
    de não citar letras, e que o texto vai ficar desatualizado após o
    embaralhamento de posições feito por balancear_gabaritos().
    """
    padrao = re.compile(r'\b(alternativa|op[cç][aã]o|item|letra)\s+[A-E]\b', re.IGNORECASE)
    return bool(padrao.search(comentario or ""))


def _embaralhar_questao(q: dict, posicao_alvo: int):
    """
    Reposiciona a alternativa correta para 'posicao_alvo' (0=A ... 4=E),
    embaralhando as demais nas posições restantes — e resolve TODOS os
    marcadores [[LETRA_OPCAO_N]] do comentário pela letra FINAL de cada
    alternativa (N é a posição ORIGINAL, 1-indexada, em que a IA gerou
    aquela alternativa, fixa desde a geração).
    """
    opcoes_originais = list(q["opcoes"])
    indice_correta_original = q["correta"]

    # Monta a nova ordem: a correta vai para 'posicao_alvo'; as outras 4
    # (identificadas pelo índice ORIGINAL) são embaralhadas nas posições
    # restantes.
    indices_restantes = [i for i in range(5) if i != indice_correta_original]
    random.shuffle(indices_restantes)

    ordem_final = [None] * 5  # ordem_final[posição_final] = índice ORIGINAL
    ordem_final[posicao_alvo] = indice_correta_original
    it_restantes = iter(indices_restantes)
    for k in range(5):
        if ordem_final[k] is None:
            ordem_final[k] = next(it_restantes)

    mapa = {0: 'A', 1: 'B', 2: 'C', 3: 'D', 4: 'E'}

    q["opcoes"] = [opcoes_originais[indice_original] for indice_original in ordem_final]
    q["correta"] = posicao_alvo
    q["letra_gabarito"] = mapa[posicao_alvo]

    # índice ORIGINAL -> letra FINAL (depois do embaralhamento)
    letra_final_por_indice_original = {
        indice_original: mapa[posicao_final]
        for posicao_final, indice_original in enumerate(ordem_final)
    }

    # Resolve cada um dos 5 marcadores possíveis pela letra final — é aqui
    # que a mágica acontece, em Python puro, sem gastar nenhuma chamada
    # extra de API. Um comentário pode citar 0, 1 ou vários marcadores
    # (ex: "a correta é [[LETRA_OPCAO_3]], diferente de [[LETRA_OPCAO_1]] e
    # [[LETRA_OPCAO_5]]") — todos são resolvidos na mesma passada.
    comentario = q.get("comentario", "") or ""
    algum_marcador_encontrado = False
    for indice_original, letra_final in letra_final_por_indice_original.items():
        marcador = marcador_opcao(indice_original)
        if marcador in comentario:
            algum_marcador_encontrado = True
            comentario = comentario.replace(marcador, letra_final)
    q["comentario"] = comentario

    if not algum_marcador_encontrado and _comentario_cita_letra(comentario):
        # Rede de segurança: a IA escapou da regra e escreveu uma letra de
        # verdade em vez de marcador. Não corrigimos automaticamente
        # (arriscado — não sabemos a qual alternativa original aquela letra
        # se referia), só avisamos nos logs pra revisão manual pontual.
        pergunta_resumida = (q.get("pergunta", "") or "")[:60]
        print(
            f"  ⚠️ [ATENÇÃO] Comentário citou uma letra literal em vez de "
            f"marcador — pode estar desatualizado após o embaralhamento. "
            f"Pergunta: \"{pergunta_resumida}...\""
        )

def balancear_gabaritos(questoes: list) -> list:
    """Distribui uniformemente as respostas entre A-E[cite: 1]."""
    n = len(questoes)
    if n == 0: return questoes
    posicoes = [i % 5 for i in range(n)]
    random.shuffle(posicoes)
    for q, pos in zip(questoes, posicoes):
        _embaralhar_questao(q, pos)
    return questoes


# ============================================================
# COMPARAÇÃO DE TEMAS (pra reaproveitamento entre estudos)
# ============================================================

SCHEMA_COMPARACAO = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "indice_parecido": {
            "type": ["integer", "null"],
        },
        "justificativa": {"type": "string"},
    },
    "required": ["indice_parecido", "justificativa"],
}


def comparar_temas_similares(tema_novo: str, candidatos: list) -> int | None:
    """
    Pergunta (com o modelo mais barato) se algum dos temas candidatos é
    parecido o suficiente com 'tema_novo' pra reaproveitar as mesmas
    questões, em vez de gerar do zero. Devolve o ÍNDICE do candidato mais
    parecido (posição na lista 'candidatos', 0-based) ou None se nenhum servir.
    """
    if not candidatos:
        return None

    lista_candidatos = "\n".join(f"{i}. {c}" for i, c in enumerate(candidatos))
    prompt = f"""Você é um especialista em conteúdo programático de concursos públicos.

TEMA NOVO (que precisa de questões):
"{tema_novo}"

TEMAS CANDIDATOS (de outros concursos, já com questões prontas):
{lista_candidatos}

Algum desses candidatos cobre o MESMO CONTEÚDO e a MESMA PROFUNDIDADE do
tema novo, a ponto de as mesmas questões servirem sem soar deslocadas?
Considere só equivalência de CONTEÚDO (ex: "Concordância verbal e nominal"
e "5.5 Concordância verbal e nominal (regras gerais)" são o mesmo assunto).
Seja rigoroso: na dúvida, prefira dizer que nenhum serve (responda null).

Responda em JSON, com "indice_parecido" (o número do candidato mais
parecido, ou null se nenhum servir de verdade) e "justificativa" (uma frase
curta explicando a decisão)."""

    resultado = _responder_json(
        prompt=prompt,
        schema=SCHEMA_COMPARACAO,
        nome_schema="comparacao_temas",
        reasoning_effort="low",
        modelo=MODEL_COMPARACAO,
    )

    indice = resultado.get("indice_parecido")
    if indice is None:
        return None
    if not isinstance(indice, int) or not (0 <= indice < len(candidatos)):
        return None
    return indice

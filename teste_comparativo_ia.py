"""
Comparação lado a lado: gpt-5.6-terra (OpenAI) vs DeepSeek Reasoner/V4-Pro, usando o MESMO
prompt que o app já usa de verdade (ia_gerador.montar_prompt) — pra isolar
a variável "qual modelo" e não misturar com "prompt diferente".

Usa response_format json_object (chat.completions) pros DOIS provedores —
mais simples e universal que a Responses API + strict schema, então não
arrisca a comparação falhar por incompatibilidade de recurso específico da
OpenAI que a DeepSeek ainda não tenha, de propósito, pra focar a avaliação
na QUALIDADE do conteúdo gerado, não em detalhe de API.

Uso:
    1. Crie um arquivo .env na mesma pasta com:
         OPENAI_API_KEY=sk-...
         DEEPSEEK_API_KEY=sk-...
    2. pip install openai python-dotenv
    3. python teste_comparativo_ia.py

Gera um arquivo comparativo_resultado.json com as questões dos dois
modelos, pros mesmos 4 temas — depois é só me mandar esse arquivo de volta.
"""
import json
import os

from dotenv import load_dotenv
from openai import OpenAI

from ia_gerador import montar_prompt  # reaproveita o prompt real do app, sem duplicar

load_dotenv()

BANCA = "FGV"
NIVEL = "Superior"
QTD_POR_TEMA = 2  # gera 2 questões por tema, de cada modelo

# 4 temas representativos dos pontos mais sensíveis a erro (reais, do config da DATAPREV)
TEMAS_TESTE = [
    "2 Lógica de argumentação: analogias, inferências, deduções e conclusões",
    "5.5 Concordância verbal e nominal",
    "4 Lei nº 13.709/2018 (LGPD): capítulos I, II, III, IV, VII, VIII e IX",
    "Conceitos de probabilidade: modelo, probabilidade condicional, independência, "
    "variáveis aleatórias, esperança/variância/covariância, distribuições contínuas e discretas",
]

MODELOS = {
    "gpt-5.6-terra": {
        "api_key": os.environ.get("OPENAI_API_KEY"),
        "base_url": None,  # padrão da OpenAI
        "parametro_limite": "max_completion_tokens",  # modelos novos da OpenAI exigem esse nome
        "max_tokens": 12000,
        "timeout": 60.0,
    },
    "deepseek-v4-pro": {
        "api_key": os.environ.get("DEEPSEEK_API_KEY"),
        "base_url": "https://api.deepseek.com",
        "parametro_limite": "max_tokens",
        # Modelos com raciocínio encadeado geram tokens de reflexão (thought tokens)
        # antes da saída final, exigindo uma margem maior de tokens e timeout ampliado.
        "max_tokens": 16000,
        "timeout": 120.0,
    },
}


def gerar_com_modelo(nome_modelo: str, config_modelo: dict, tema: str) -> list:
    cliente = OpenAI(
        api_key=config_modelo["api_key"],
        base_url=config_modelo["base_url"],
        timeout=config_modelo.get("timeout", 60.0),
    )
    prompt = montar_prompt(BANCA, NIVEL, tema, QTD_POR_TEMA, evitar=None)
    # response_format=json_object exige que a palavra "JSON" apareça em
    # algum lugar da mensagem — nosso prompt real não menciona isso
    # literalmente, então adicionamos essa instrução curta à parte.
    prompt_com_instrucao_json = (
        prompt + '\n\nResponda em formato JSON, com uma chave "questoes" '
        "contendo a lista de questões geradas."
    )

    kwargs_limite = {config_modelo["parametro_limite"]: config_modelo.get("max_tokens", 12000)}
    resposta = cliente.chat.completions.create(
        model=nome_modelo,
        messages=[{"role": "user", "content": prompt_com_instrucao_json}],
        response_format={"type": "json_object"},
        **kwargs_limite,
    )
    texto = resposta.choices[0].message.content
    motivo_parada = resposta.choices[0].finish_reason
    if motivo_parada == "length":
        print(f"    ⚠️ [{nome_modelo}] parou por estourar o limite de tokens (finish_reason=length)")
    if not texto:
        raise ValueError(f"Resposta vazia (finish_reason={motivo_parada})")
    dados = json.loads(texto)
    return dados.get("questoes", [])


def main():
    resultado = {}
    for tema in TEMAS_TESTE:
        print(f"\n=== TEMA: {tema[:60]}... ===")
        resultado[tema] = {}
        for nome_modelo, config_modelo in MODELOS.items():
            if not config_modelo["api_key"]:
                print(f"  [{nome_modelo}] pulado — sem API key configurada no .env")
                continue
            try:
                print(f"  Gerando com {nome_modelo}...")
                questoes = gerar_com_modelo(nome_modelo, config_modelo, tema)
                resultado[tema][nome_modelo] = questoes
                print(f"  [{nome_modelo}] ✓ {len(questoes)} questão(ões) geradas.")
            except Exception as e:
                print(f"  [{nome_modelo}] ❌ ERRO: {e}")
                resultado[tema][nome_modelo] = {"erro": str(e)}

    with open("comparativo_resultado.json", "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)

    print("\n\nPronto! Resultado salvo em comparativo_resultado.json — me manda esse arquivo.")


if __name__ == "__main__":
    main()

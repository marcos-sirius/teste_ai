"""
Teste mínimo: só confere se a chave da DeepSeek está válida, sem gerar
questão nenhuma. Uma chamada só, resposta bem curta.
"""
import os

from openai import OpenAI

api_key = os.environ.get("DEEPSEEK_API_KEY")

if not api_key:
    print("❌ DEEPSEEK_API_KEY não está definida no ambiente.")
    raise SystemExit(1)

print(f"Testando chave (começa com: {api_key[:8]}...)")

cliente = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

try:
    resposta = cliente.chat.completions.create(
        model="deepseek-flash",
        messages=[{"role": "user", "content": "Responda apenas: ok"}],
        max_tokens=10,
    )
    print("✅ Chave válida! Resposta recebida:", resposta.choices[0].message.content)
except Exception as e:
    print("❌ Falhou:", e)
    raise SystemExit(1)

"""Read-only fictional inbox; no real email account is accessed."""
from mcp.server.mcpserver import MCPServer

app = MCPServer("Caixa de email demonstrativa")

@app.tool()
def verificar_caixa_email(caixa: str = "demo") -> dict:
    """Consulta emails FICTICIOS da caixa demo. Nao acessa emails reais."""
    if caixa.lower() not in ("demo", "x"):
        return {"erro": "Somente as caixas ficticias demo ou X estao disponiveis."}
    return {"simulado": True, "caixa": caixa, "mensagens": [
        {"remetente": "Ana (ficticio)", "assunto": "Reuniao de validacao", "resumo": "Confirmar a reuniao de amanha as 10h."},
        {"remetente": "Equipe (ficticio)", "assunto": "Prototipo", "resumo": "O prototipo esta pronto para avaliacao."}
    ]}

if __name__ == "__main__":
    app.run(transport="stdio")


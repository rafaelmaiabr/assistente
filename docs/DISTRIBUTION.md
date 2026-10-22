# Distribuição Windows — issue #6

## Usar o pacote portable

Extraia a pasta inteira e abra **Assistente.exe**. Não mova somente o executável: DLLs e a pasta `resources` são necessários. Node.js e um terminal não são necessários para operar o pacote. O aplicativo inclui Electron, uv e os fontes do backend; na primeira execução o uv prepara Python 3.13 e as dependências na pasta de dados do usuário. Isso exige internet e espaço em disco. O pacote de validação não é assinado digitalmente.

Ollama deve estar instalado separadamente, com o modelo definido em `config.json` (padrão `qwen2.5:7b`). Não são distribuídos pesos de modelos. Se o Ollama local estiver parado, o aplicativo tenta iniciá-lo; se não o encontrar ou se faltar o modelo, mostra orientação e permite tentar novamente. O botão **Instalar Ollama** abre apenas a página oficial. **Abrir configuração** abre a pasta de dados para ajustar o modelo/provedor. Serviços já em execução são reutilizados.

Na distribuição, configuração, ambiente Python e cache Whisper ficam em `data` dentro da pasta de dados do aplicativo Electron, normalmente `%APPDATA%\assistente-local\data`. No checkout, continuam na raiz do projeto. A variável `ASSISTENTE_DATA_DIR` permite escolher outra pasta de dados antes de iniciar. Nunca copie config.json, .models ou logs para um release.

**Ligar microfone** inicia captura após ação explícita; escolha o microfone físico e acompanhe o medidor. **Silenciar** encerra a captura. Fechar o chat apenas o oculta: o avatar permanece visível e indica a captura ativa. **×** no avatar encerra o aplicativo e os processos que ele criou. Um Ollama ou servidor web que já estava ativo antes da inicialização é preservado.

## Diagnóstico

- **Ollama não encontrado:** instalar pelo botão da tela e tentar novamente.
- **Modelo não instalado:** a tela informa os modelos encontrados; ajustar `model` em config.json ou instalar o modelo no Ollama.
- **Configuração inválida:** abrir a pasta, corrigir o JSON e tentar novamente.
- **Servidor antigo/porta ocupada:** encerrar a instância anterior. No checkout, usar `encerrar-web.cmd` para o servidor deste projeto.
- **Permissão negada ou áudio indisponível:** verificar a seleção do microfone no chat e o acesso concedido ao aplicativo. A captura começa desligada em toda nova sessão.
- **Desconexão:** a captura para e o chat oferece **Nova conversa**. Se o servidor tiver sido encerrado, fechar o aplicativo pelo avatar e reabrir; o runtime verifica/inicia o serviço novamente. O histórico da sessão anterior não é recuperado.

## Gerar um pacote

Em Windows x64, com Node.js 22+, uv e as dependências de build:

```powershell
npm ci --prefix frontend
npm run build --prefix frontend
npm ci --prefix desktop
npm test --prefix desktop
npm run package --prefix desktop
```

Saída: `release/Assistente-win32-x64/Assistente.exe`. O build usa uma lista explícita de arquivos e inclui um `build-manifest.json` com versões e os arquivos do backend. Configuração local, modelos, históricos, áudio, logs e ambientes virtuais não entram no pacote. O uv inclui a licença MIT oficial; Electron inclui suas licenças no pacote. A workflow **Windows portable package** pode ser executada manualmente no GitHub e disponibiliza a pasta completa como artefato.

O ambiente Python é preparado por versão/hash dos requirements. Uma atualização pode auditar/atualizar dependências; não sobrescreve a configuração do usuário. A distribuição é para validação, com texto/voz e avatar; email real, memória persistente e roteamento automático de modelos não fazem parte das issues #4–#6.

# Akuma Daemon

O Akuma Daemon é o único processo do Akuma que deve ser registrado como serviço do sistema operacional. Ele atua como supervisor: inicia, monitora, reinicia, habilita, desabilita e encerra os serviços internos.

O primeiro serviço interno é o `task-scheduler`, responsável por tarefas, recorrências, execução de scripts e histórico. O SQLite desse serviço fica em `configs/daemon/services/task-scheduler/`; o endpoint e o estado do supervisor ficam em `configs/daemon/supervisor/`. Esses dados são privados e estão protegidos pelo `.gitignore` da raiz.

## Desenvolvimento

```powershell
py -3 -m pip install -e ".[test]"
py -3 -m pytest -q
```

Para executar o supervisor em foreground:

```powershell
py -3 -m akuma_daemon run
```

Em outro terminal, a CLI controla o supervisor e encaminha as operações ao serviço interno:

```powershell
py -3 -m akuma_daemon list-services
py -3 -m akuma_daemon add email-check python --arg=-m --arg=my_script --interval 3600
py -3 -m akuma_daemon list
py -3 -m akuma_daemon pause email-check
py -3 -m akuma_daemon resume email-check
py -3 -m akuma_daemon run-now email-check
py -3 -m akuma_daemon history email-check
py -3 -m akuma_daemon remove email-check
```

Comandos de ciclo de vida dos serviços internos:

```powershell
py -3 -m akuma_daemon start task-scheduler
py -3 -m akuma_daemon stop task-scheduler
py -3 -m akuma_daemon restart task-scheduler
py -3 -m akuma_daemon enable task-scheduler
py -3 -m akuma_daemon disable task-scheduler
py -3 -m akuma_daemon status task-scheduler
```

## Telegram Gateway

O Telegram Gateway é iniciado pelo supervisor como o serviço interno `telegram`. Cada bot reside em `configs/telegram/bots/<bot_id>/`, com `bot.json`, `contacts.json`, `vault/`, `state/` e `staging/` próprios. O diretório de serviço em `configs/daemon/services/telegram/` mantém somente o endpoint e a configuração do processo. Tokens e senhas continuam resolvidos em tempo de execução e nunca são armazenados no JSON do bot.

```powershell
py -3 -m akuma_daemon telegram bots
py -3 -m akuma_daemon telegram start-listener bot-principal
py -3 -m akuma_daemon telegram pair-request bot-principal
py -3 -m akuma_daemon telegram owners bot-principal
py -3 -m akuma_daemon telegram send bot-principal 123456789 "Mensagem de teste"
py -3 -m akuma_daemon telegram send bot-principal -100123456789 "Mensagem no tópico" --thread-id 42
py -3 -m akuma_daemon telegram migrate-bots --dry-run
py -3 -m akuma_daemon telegram migrate-bots --apply
```

O listener responde inicialmente a qualquer mensagem com `Não incomode o Akuma`. O comando `/pair 123456` (também aceito como `/painr` por compatibilidade) consome o PIN único emitido por `pair-request` e registra o remetente como owner do bot.

### TOTP efêmero

Com `totp.enabled` ativo no JSON do bot, somente um owner em conversa privada pode usar `/totp [filtro]`. O bot pede a senha, apaga a mensagem recebida e compara os valores dos caminhos `real_password_entry` e `fake_password_entry` da seção `[totp]` do perfil. A senha falsa responde que não há TOTPs; uma senha inválida é recusada; a senha real exibe entradas TOTP paginadas. O filtro não diferencia maiúsculas/minúsculas e `*` representa qualquer sequência no caminho da entrada.

O perfil reutiliza a seção `[vault]` existente e acrescenta:

```ini
[totp]
real_password_entry = Akuma/Telegram/TOTP real
fake_password_entry = Akuma/Telegram/TOTP falso
```

As mensagens do fluxo — solicitação, lista, avisos e código — são apagadas automaticamente. O código é enviado sozinho, sem proteção de conteúdo, para facilitar a cópia; a mensagem separada informa sua expiração. Ambas são apagadas cinco segundos após a expiração. Enquanto consulta o Vault ou calcula o código, o gateway renova o indicador `typing`. O período é configurável em `totp.period_seconds` e deve coincidir com o período das entradas TOTP, cujo padrão é 30 segundos.

O gateway configura `/totp` no menu do bot somente no escopo individual da conversa privada de cada owner. Essa sincronização ocorre ao iniciar ou reiniciar o listener e após um pairing bem-sucedido; o gateway nunca configura esse comando em escopos globais, privados gerais ou de grupos.

### Agente Codex por bot

O bot principal usa `agent.context=akuma`: sem `codex_home`, herda o HOME do serviço; sem `working_directory`, trabalha explicitamente na raiz do Akuma. Configurações legadas sem `context` também são tratadas como `akuma`.

Um bot restrito usa `agent.context=subbot`. `agent-init` cria `codexhome`, `codexwork`, state, staging e o perfil de Vault ausentes; `agent-sync` cria somente os links de skills declarados e registrados; `agent-login` autentica interativamente o HOME daquele bot; `agent-validate` bloqueia a inicialização quando HOME, CWD, Vault, autenticação, isolamento ou capacidade do App Server não satisfazem o contrato.

```powershell
py -3 -m akuma_daemon telegram agent-init subbot-exemplo
py -3 -m akuma_daemon telegram agent-sync subbot-exemplo
py -3 -m akuma_daemon telegram agent-login subbot-exemplo
py -3 -m akuma_daemon telegram agent-status subbot-exemplo
py -3 -m akuma_daemon telegram agent-validate subbot-exemplo --require-login
```

Cada subbot recebe `project_root_markers = []`, é iniciado fisicamente em `codexwork` e repete esse CWD em `thread/start`, `thread/resume` e `turn/start`. O gateway valida `instructionSources` em toda criação e retomada e encerra o fluxo se uma instrução externa ao workspace aparecer. HOME, workspace, Vault, state e staging não podem se sobrepor. A leitura restrita é fail-closed: uma versão do App Server sem raízes de leitura restritas não inicia subbots.

O perfil `vault/keepass.ini` contém apenas `cli_path`, `database_path` e `timeout_seconds`. `vault.access` é obrigatório. O processo recebe `KEEPASS_VAULT_CONFIG`, `KEEPASS_VAULT_AUTH_MODE`, `KEEPASS_VAULT_AUTH_TARGET` e `KEEPASS_VAULT_ACCESS`; o wrapper recusa operações mutáveis quando o acesso é `read_only`.

#### Instruções do agente e capacidades do Telegram

O gateway monta `developerInstructions` em camadas. A primeira camada é o contrato obrigatório e versionado `telegram-v1`, que ensina o agente a operar com segurança dentro de uma DM. A segunda descreve as capacidades realmente registradas no App Server. A terceira contém as instruções particulares do bot; a última identifica o escopo de execução. O bot pode declarar as instruções particulares diretamente em `agent.instructions.developer` ou em um arquivo UTF-8 interno ao diretório do bot por `agent.instructions.developer_file`, nunca ambos:

```json
"instructions": {
  "gateway_profile": "telegram-v1",
  "developer_file": "developer.md"
}
```

O arquivo particular é apropriado para objetivo, limites de negócio, tom e personalidade detalhada do bot. O campo opcional `agent.personality` aceita apenas as personalidades nativas `friendly` e `pragmatic`; regras particulares continuam no bloco de instruções.

O gateway habilita a API experimental do App Server e registra, no namespace `telegram_gateway`, as ferramentas `send_message`, `ask_menu`, `list_attachments` e `materialize_attachment`. O agente pode enviar uma mensagem separada com TTL persistente, apresentar uma escolha inline e consultar ou materializar anexos anteriores da conversa. O gateway determina bot, owner, chat e turno a partir do contexto ativo; essas identidades nunca são argumentos controlados pelo modelo. Menus aceitam somente callback do owner na mesma DM e são apagados na seleção ou no timeout.

O SQLite guarda um hash do contrato composto. Qualquer alteração nas instruções ou no schema das ferramentas desvincula e exclui a thread anterior antes do próximo turno, evitando retomar uma conversa criada com capacidades incompatíveis. Anexos são arquivados no state exclusivo do bot até `/new`; quando requisitados, são copiados temporariamente para o staging e removidos ao final do turno. `/new` elimina também o arquivo retido da geração anterior.

### Conversas privadas

No MVP, somente DMs de um contato com role `owner` no `contacts.json` daquele bot chegam ao Codex. Grupos, tópicos e remetentes não autorizados são silenciosamente ignorados. Comandos nativos e callbacks são processados antes do agente. Fotos são entregues como `localImage`; documentos são baixados para o staging exclusivo do bot, com limites padrão de 20 MB por arquivo, 50 MB por lote e 50 itens pendentes.

O SQLite exclusivo do bot persiste a thread por conversa, configurações, inbox e deduplicação de updates. Mensagens que chegam durante um turno formam o lote do próximo turno, em ordem. Conversas diferentes executam em paralelo até `max_parallel_conversations`; uma mesma conversa permanece serializada. Itens já iniciados são marcados como abandonados após queda e não são repetidos automaticamente.

#### Transcrição de mensagens de voz

Um bot pode habilitar STT nativo do gateway somente para mensagens `voice` do Telegram; arquivos enviados como `audio` ou `document` não entram nesse fluxo. O gateway baixa primeiro o OGG/Opus para o staging e o arquiva como anexo da conversa. Em seguida, no worker serializado da conversa e antes do turno Codex, envia o arquivo ao EccoVox local:

```json
"voice_transcription": {
  "enabled": true,
  "provider": "eccovox",
  "base_url": "http://127.0.0.1:8870",
  "request_timeout_seconds": 120,
  "queue_timeout_seconds": 180,
  "language": "pt-BR",
  "profile": "balanced",
  "max_audio_bytes": 10485760
}
```

`base_url` aceita somente HTTP loopback (`127.0.0.1`, `localhost` ou `::1`) e porta explícita; o gateway desabilita proxy e redirecionamentos. Isso preserva a natureza local do EccoVox, que não expõe autenticação HTTP. As chamadas são serializadas por endpoint porque a instalação padrão do EccoVox atende uma transcrição por vez.

Em sucesso, o turno recebe somente a tag curta `[Transcrição de mensagem de áudio recebida pelo Telegram]` seguida do texto. As instruções obrigatórias do gateway, enviadas uma vez por thread, explicam que a transcrição pode conter erros e exigem que o agente deixe claro ao usuário como interpretou informação ambígua. O áudio original não aparece como texto no prompt, mas continua disponível como anexo pelos recursos do gateway. Se a transcrição falhar, o turno recebe um marcador curto e conserva o anexo original; nenhuma transcrição é enviada como mensagem visível ao Telegram ou gravada nos logs.

`/new` incrementa a geração da conversa, interrompe o turno corrente, limpa a fila e o staging, apaga a thread remota e desvincula o mapeamento mesmo se a exclusão remota falhar. `/config` controla o compartilhamento e a exclusão dos pensamentos; ambos vêm ligados. Summaries de reasoning e commentary público são enviados em mensagens `💭` deduplicadas e, quando configurado, apagados ao finalizar, falhar ou cancelar. O indicador `typing` é renovado a cada quatro segundos durante downloads e turnos.

O adaptador de Windows Service usa o mesmo supervisor e requer `pywin32`:

```powershell
py -3 -m pip install -e ".[windows]"
py -3 -m akuma_daemon.windows_service install
```

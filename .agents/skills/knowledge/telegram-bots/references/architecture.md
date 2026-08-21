# Arquitetura e isolamento

## Perfis de agente

O gateway suporta vários bots identificados por `bot_id`. Cada bot representa uma identidade Telegram e pode possuir um App Server Codex próprio.

### Bot principal

O contexto `akuma` preserva o comportamento do agente principal. Sem `codex_home`, herda o HOME do serviço. Sem `working_directory`, usa explicitamente a raiz do projeto Akuma. Configurações legadas sem `agent.context` são tratadas como `akuma`.

### Subbot

O contexto `subbot` usa por padrão `codexhome` e `codexwork` dentro da raiz privada do bot. Seu HOME contém configuração, autenticação, sessões e skills declaradas; seu workspace contém `AGENTS.md` e os arquivos do projeto próprio. Um subbot não herda automaticamente contatos, Vault, skills, instruções, configuração, sessões ou workspace do Akuma.

## Organização privada por bot

Usar a estrutura:

```text
configs/telegram/bots/<bot_id>/
├── bot.json
├── contacts.json
├── vault/
│   └── keepass.ini
├── codexhome/          # subbot
├── codexwork/          # subbot
├── state/
│   └── gateway.sqlite3
└── staging/
```

Não permitir que `codexhome`, `codexwork`, `vault`, `state` e `staging` sejam iguais, sobrepostos ou descendentes uns dos outros. O KDBX pode permanecer fora dessa árvore, mas deve ser declarado exclusivamente no perfil próprio. Permitir dois bots no mesmo KDBX somente por configuração explícita em ambos.

## Configuração do bot

O `bot.json` separa `listener`, `totp`, `agent` e `vault`. Para subbots, definir no mínimo identidade, executável Codex, modelo, esforço, sandbox, política de aprovação, rede, timeout, paralelismo, limites de fila, filesystem e skills. Exigir `vault.access` com valor `read_only` ou `read_write`.

Nunca gravar token do Telegram, senha do KDBX ou segredo de autenticação no JSON. O campo `profile` referencia a configuração que resolve o token de forma segura. O bloco `vault.auth` referencia `mode=windows_credential_manager` e o target próprio do bot.

## Isolamento do Codex

Iniciar fisicamente o App Server no `codexwork` e repetir o mesmo `cwd` em `thread/start`, `thread/resume` e `turn/start`. Configurar `project_root_markers = []` no HOME do subbot e rejeitar qualquer `instructionSources` externo ao workspace.

Usar `workspace-write`, aprovação `never` e rede desabilitada como base do subbot. O sandbox limita tecnicamente os processos iniciados pelo agente; instruções de prompt são apenas defesa adicional.

### Limitação atual confirmada

Os binários públicos do App Server avaliados não expõem `workspaceWrite.readOnlyAccess` no schema gerado, embora a documentação descreva o campo. A implementação atual do gateway falha fechada quando `filesystem.read_access=restricted` depende dessa capacidade.

### Decisão pendente de implementação: fallback sem `readOnlyAccess`

Não bloquear a evolução esperando uma versão futura. Usar o sandbox nativo elevado do Windows e remover do agente a necessidade de ler raízes externas diretamente:

- configurar `[windows] sandbox = "elevated"` no HOME do subbot;
- manter workspace e staging como únicas áreas de trabalho acessíveis;
- sincronizar skills declaradas como cópias gerenciadas no ambiente do subbot, evitando symlinks para o Akuma;
- oferecer Vault por ferramenta controlada do gateway, vinculada ao perfil fixo do bot, em vez de entregar livre acesso ao KDBX ao shell;
- tratar outros conteúdos externos como cópias gerenciadas ou ferramentas específicas;
- validar por canários reais que o subbot não lê a raiz do Akuma nem os dados de outro bot.

Não substituir essa decisão por `read-only`: esse modo elimina gravação necessária no workspace e não resolve sozinho o acesso controlado a recursos externos.

## Skills

Compartilhar somente skills declaradas no `bot.json`. O provisionador deve criar apenas recursos ausentes, registrar os recursos gerenciados e remover somente aqueles que ele próprio criou. Até o fallback acima ser implementado, o código atual usa links gerenciados; considerar esse comportamento transitório e verificar o alvo antes de operar.

## Vault

Cada bot possui `vault/keepass.ini` próprio com apenas caminho do KeePassXC CLI, caminho do KDBX e timeout. Validar pelo `config_tool.py validate`. Passar `KEEPASS_VAULT_CONFIG`, `KEEPASS_VAULT_AUTH_MODE`, `KEEPASS_VAULT_AUTH_TARGET` e `KEEPASS_VAULT_ACCESS` ao processo apropriado.

Em `read_only`, recusar operações mutáveis no wrapper e no limite técnico de acesso. Em `read_write`, autorizar somente o KDBX declarado. Como o Credential Manager pertence à identidade Windows, não assumir que outra conta ou usuário sandbox possua a credencial.

## Contatos e owners

Cada bot possui `contacts.json` independente. Toda consulta de autorização deve receber o `bot_id`. Um usuário pode ser owner de um bot e inexistente nos demais. Pairing modifica somente o arquivo do bot receptor. A migração do campo legado `owners` deve criar backup e remover o campo do JSON principal atomicamente.

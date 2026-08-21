# Provisionamento e produção

## Definição passo a passo de um novo subbot

Coletar uma decisão por vez para evitar configuração contraditória. Não pedir a próxima informação antes de registrar e validar a resposta atual.

Ordem recomendada:

1. Identidade: nome, `bot_id` normalizado e especialidade.
2. Identidade Telegram: bot criado no BotFather e referência segura para o token; nunca pedir o token em texto aberto quando houver armazenamento seguro disponível.
3. Owners: quem poderá parear e administrar o bot.
4. Missão e limites: tarefas permitidas, tarefas recusadas e tom de comunicação.
5. Contexto Codex: confirmar `subbot`, executável, modelo e esforço.
6. Workspace: arquivos iniciais, instruções próprias e necessidade de persistência de projeto.
7. Skills: declarar apenas as capacidades indispensáveis.
8. Vault: KDBX, access `read_only` ou `read_write` e target próprio no Credential Manager.
9. Rede e filesystem: manter rede desligada e definir qualquer necessidade externa por ferramenta controlada ou cópia gerenciada.
10. Limites operacionais: timeout, paralelismo, fila e anexos.
11. Provisionamento, login, validação, teste por DM e ativação do listener.

Não criar credenciais, contas, ACLs ou alterações externas destrutivas sem autorização explícita. Quando a resposta implicar escolha de segurança relevante, explicar o impacto antes de seguir para a pergunta seguinte.

## Comandos administrativos

Usar:

```powershell
py -3 -m akuma_daemon telegram agent-init <bot_id>
py -3 -m akuma_daemon telegram agent-sync <bot_id>
py -3 -m akuma_daemon telegram agent-login <bot_id>
py -3 -m akuma_daemon telegram agent-status <bot_id>
py -3 -m akuma_daemon telegram agent-validate <bot_id> --require-login
```

Para legado:

```powershell
py -3 -m akuma_daemon telegram migrate-bots --dry-run
py -3 -m akuma_daemon telegram migrate-bots --apply
```

Executar dry-run antes de apply. Preservar backups e não sobrescrever arquivos não gerenciados.

## Provisionamento

`agent-init` deve criar somente recursos ausentes: diretórios privados, HOME mínimo, workspace, `AGENTS.md`, contatos e perfil Vault. `agent-sync` deve materializar somente skills declaradas e registrar tudo que gerencia. `agent-login` deve autenticar o HOME exclusivo do bot sem expor credenciais. Nenhum comando deve copiar automaticamente o HOME ou as skills do Akuma.

## Validação obrigatória

Antes da produção, confirmar:

- `bot_id`, estrutura e caminhos sem sobreposição;
- `contacts.json` exclusivo e owners corretos;
- token Telegram resolvido sem segredo em arquivo ou log;
- HOME autenticado e App Server iniciado no CWD correto;
- perfil de instruções `telegram-v1`, instruções particulares legíveis e hash do contrato persistido;
- `project_root_markers = []` e `instructionSources` somente do workspace;
- skills declaradas e nenhum recurso herdado indevidamente;
- perfil KeePass válido, KDBX correto, target próprio e enforcement de `vault.access`;
- leitura e escrita permitidas somente onde declarado;
- leitura da raiz do Akuma e dos dados de outro bot efetivamente negada;
- rede negada quando configurada;
- SQLite, staging e threads sem cruzamento entre bots;
- DM de owner aceita e owner de outro bot ignorado;
- `/new`, `/config`, pensamentos, typing, anexos retidos, mensagens efêmeras, menus inline e reinício;
- `send_message`, `ask_menu`, `list_attachments` e `materialize_attachment` disponíveis somente na DM ativa;
- TOTP somente quando explicitamente habilitado e configurado.

Não aceitar uma validação baseada apenas na configuração declarada. Executar canários reais de acesso e verificar o retorno do App Server.

## Rollout

1. Manter o bot principal ativo e inalterado.
2. Provisionar o subbot com listener desabilitado.
3. Executar login e validação completa.
4. Iniciar o listener e realizar pairing por DM.
5. Confirmar comandos privados somente para os owners daquele bot.
6. Testar conversa, retomada de thread, fila, `/new`, anexos e reinício.
7. Observar logs sem conteúdo de prompts, respostas ou segredos.
8. Habilitar produção somente após todos os testes passarem.

## Diagnóstico

Ao falhar, reportar o estágio preciso: descoberta da configuração, resolução do token, API Telegram, autorização, App Server, autenticação Codex, sandbox, instruction sources, Vault, fila ou staging. Preservar mensagens técnicas seguras e códigos de retorno; não reduzir falhas diferentes a uma resposta genérica.

Se o comportamento divergir desta referência, verificar primeiro o código e os testes atuais, reportar a inconsistência e atualizar esta skill somente após confirmação.

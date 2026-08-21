---
name: telegram-bots
description: Conhecimento arquitetural e operacional do gateway Telegram do Akuma, incluindo bots e subbots Codex, isolamento de HOME e workspace, contatos e owners por bot, Vault individual, roteamento de DMs, threads persistentes, comandos nativos, TOTP efêmero, provisionamento, validação e produção. Use sempre que a tarefa envolver criar, configurar, migrar, diagnosticar, testar, operar ou evoluir um bot ou subbot do Telegram conectado ao Akuma.
---

# Bots e subbots do Telegram

Usar esta skill como mapa do domínio do Telegram Gateway. Distinguir sempre o comportamento implementado das decisões arquiteturais ainda não incorporadas ao código.

## Fonte de verdade

Antes de alterar ou operar o gateway:

1. Ler `akuma-daemon/src/akuma_daemon/telegram_manager.py` para roteamento, pairing, TOTP, callbacks e ciclo dos listeners.
2. Ler `akuma-daemon/src/akuma_daemon/telegram_agent.py` para App Server, isolamento, persistência, fila, anexos, pensamentos e provisionamento.
3. Ler o `bot.json` e o `contacts.json` do bot afetado sem expor tokens, credenciais ou dados pessoais.
4. Consultar as referências desta skill conforme o assunto.
5. Tratar divergência entre esta skill e o código como conhecimento desatualizado: reportar a divergência e confirmar o comportamento por testes antes de operar produção.

## Referências

- Ler [architecture.md](references/architecture.md) ao criar bots, definir HOME, workspace, skills, Vault, sandbox, contatos ou limites de isolamento.
- Ler [routing-and-conversations.md](references/routing-and-conversations.md) ao trabalhar com autorização, DMs, comandos, instruções do agente, ferramentas do gateway, threads, fila, `/new`, `/config`, pensamentos, typing ou anexos.
- Ler [totp.md](references/totp.md) ao alterar ou diagnosticar o fluxo TOTP.
- Ler [provisioning-and-production.md](references/provisioning-and-production.md) ao provisionar, validar, migrar, iniciar, reiniciar ou colocar um bot em produção.

## Regras invariantes

- Identificar primeiro qual bot recebeu o update; nunca inferir autorização, contato, Vault, estado ou configuração a partir de outro bot.
- Manter `contacts.json`, Vault, state, staging, configurações e App Server independentes por bot.
- Aceitar conversa com o agente no MVP somente em DM de contato com role `owner` naquele bot; ignorar silenciosamente grupos, tópicos e remetentes não autorizados.
- Processar comandos nativos e callbacks antes de encaminhar conteúdo ao Codex.
- Compor as instruções do Codex com o contrato obrigatório e versionado do gateway antes das instruções particulares do bot; nunca permitir que a configuração particular substitua ou remova as regras do canal.
- Resolver bot, owner, chat, conversa e turno no gateway; nunca aceitar essas identidades como argumentos controlados pelo modelo em ferramentas Telegram.
- Não armazenar tokens ou senhas em JSON, INI, argumentos, logs, prompts, workspace ou HOME. Usar referências ao Windows Credential Manager.
- Não considerar instruções, symlinks, `cwd` ou `project_root_markers` como fronteira de segurança; validar o isolamento por negações reais de acesso.
- Não liberar subbot quando `agent-validate` não comprovar o contrato de isolamento, autenticação, Vault e workspace.
- Preservar o bot principal `akuma` com o comportamento legado salvo alteração explícita.
- Fazer mudanças de produção reversíveis, validar o alvo exato e não sobrescrever recursos não gerenciados.

## Desenvolvimento do domínio

Ao inventar ou alterar uma regra reutilizável deste gateway, atualizar a referência correspondente depois de confirmar o comportamento. Registrar decisões ainda não implementadas com o rótulo `Decisão pendente de implementação`; nunca apresentá-las como garantia ativa.

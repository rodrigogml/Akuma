# Roteamento e conversas

## Crivo de entrada

Aplicar nesta ordem:

1. Identificar o bot que recebeu o update.
2. Identificar DM, grupo ou tópico.
3. Em DM, consultar exclusivamente o `contacts.json` daquele bot e aceitar o agente somente para role `owner`.
4. Em grupo ou tópico, ignorar silenciosamente no MVP.
5. Processar pairing, TOTP, `/new`, `/config` e callbacks antes do Codex.
6. Encaminhar ao agente somente tipos suportados.

O desenho futuro para grupos exige menção direta ao bot ou reply a uma mensagem do bot, além de autorização do remetente. Não habilitar esse comportamento até que exista implementação e teste específicos.

## Pairing e comandos

O pairing usa PIN único, com expiração e limite de tentativas. Armazenar somente o hash no state do bot e adicionar o remetente como owner exclusivamente naquele bot. Após pairing, sincronizar os comandos privados daquele owner.

Configurar `/new`, `/config` e `/totp` somente no escopo privado individual de cada owner. Sincronizar ao iniciar ou reiniciar o listener e após pairing. Nunca publicar esses comandos em escopo global, privado geral ou de grupo.

## Threads e persistência

Persistir a conversa no SQLite exclusivo do bot pela chave `(context_type, chat_id, message_thread_id)`. Uma DM retoma a mesma thread Codex até `/new`. Serializar uma conversa e permitir paralelismo entre conversas até `max_parallel_conversations`.

Persistir também o hash do contrato composto por instruções e schemas de ferramentas. Se o hash configurado mudar, excluir e desvincular a thread anterior antes do próximo turno. Uma thread criada sem o contrato atual não pode ser retomada silenciosamente.

Mensagens recebidas durante um turno entram na fila. Após a conclusão, enviar todo o lote pendente como próximo turno único, preservando ordem, texto, captions e anexos. Itens pendentes sobrevivem a reinício; itens já marcados como iniciados são abandonados e não são repetidos automaticamente.

Deduplicar updates por bot. Não registrar prompts nem respostas em logs.

## Instruções e capacidades do gateway

Montar `developerInstructions` nesta ordem:

1. Contrato obrigatório e versionado do gateway, atualmente `telegram-v1`.
2. Manifesto das capacidades realmente registradas no App Server.
3. Instruções particulares do bot.
4. Identificação do bot e do transporte ativo.

As instruções particulares podem ser texto em `agent.instructions.developer` ou arquivo UTF-8 interno ao diretório do bot em `agent.instructions.developer_file`, nunca ambos. Usar esse bloco para missão, limites, comportamento, objetivo e personalidade detalhada. `agent.personality` é apenas o seletor opcional das personalidades nativas suportadas pelo Codex.

Inicializar o App Server com `capabilities.experimentalApi=true` e registrar no `thread/start` o namespace dinâmico `telegram_gateway`, composto pelas funções:

- `send_message`: envia mensagem separada e, quando solicitado, registra TTL persistente para exclusão após reinício;
- `ask_menu`: envia menu inline, espera a escolha e aceita apenas callback do owner, da mesma DM e da mesma mensagem;
- `list_attachments`: lista metadados dos anexos retidos na geração corrente da conversa;
- `materialize_attachment`: copia um anexo retido para o staging exclusivo do bot durante o turno.

Derivar bot, owner, chat, conversa, geração e turno do contexto ativo mantido pelo gateway. Nenhuma ferramenta aceita essas identidades como argumento do modelo. Tratar timeout, callback inválido, falha de exclusão garantida e arquivo fora do escopo como falha real da ferramenta.

## `/new`

Ao receber `/new`:

1. Incrementar a geração da conversa para invalidar eventos atrasados.
2. Interromper o turno com `turn/interrupt`.
3. Limpar fila, anexos pendentes e pensamentos temporários.
4. Solicitar `thread/delete`.
5. Desvincular permanentemente a thread local mesmo se a exclusão remota falhar.
6. Criar uma thread nova somente na próxima mensagem.

Não reutilizar nem tentar recuperar automaticamente a thread desvinculada.

## `/config` e pensamentos

Persistir por conversa as opções `Compartilha Pensamentos` e `Excluir Pensamentos`, ambas ligadas por padrão. Encaminhar apenas summaries públicos de reasoning e commentary público, deduplicados. Enviar cada pensamento em mensagem própria iniciada por `💭`.

Quando a exclusão estiver ligada, apagar pensamentos após resposta final, erro ou cancelamento. Proteger e expirar também as mensagens do menu inline.

## Feedback de atividade

Renovar `typing` aproximadamente a cada quatro segundos durante downloads, processamento do Vault e execução ativa. Encerrar a renovação ao concluir, falhar ou cancelar.

## Anexos

Entregar fotos como `localImage`. Baixar documentos imediatamente para o staging exclusivo do bot. Aplicar inicialmente 20 MB por arquivo, 50 MB por lote e 50 itens pendentes. Remover staging após conclusão, falha, cancelamento ou descarte no reinício.

Arquivar uma cópia durável de cada anexo recebido dentro do state exclusivo do bot e associá-la à conversa e à geração. Reter essa cópia até `/new`. Ao materializar um anexo passado, copiar somente o arquivo autorizado para staging e remover essa cópia temporária ao final do turno. `/new` deve apagar o arquivo durável e seu registro.

Nunca aceitar path vindo do usuário como arquivo local e nunca permitir leitura do staging de outro bot.

## Cancelamentos e eventos atrasados

Associar eventos ativos à geração da conversa. Descartar qualquer notificação de geração antiga. Recusar approvals e solicitações interativas desconhecidas do App Server; aceitar somente `item/tool/call` das ferramentas registradas e escopadas. Limpar mensagens temporárias e staging tanto em sucesso quanto em falha.

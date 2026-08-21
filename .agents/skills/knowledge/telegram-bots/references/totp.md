# TOTP efêmero

## Escopo e precedência

Interpretar `/totp [filtro]` diretamente no gateway, sem acionar Codex CLI ou agente. Aceitar somente em DM de owner do bot receptor. Não reconhecer nem configurar TOTP em grupos, tópicos, outros bots ou usuários não autorizados.

## Fluxo

1. Apagar a mensagem do comando e pedir a senha TOTP.
2. Receber a próxima mensagem esperada como senha e apagá-la imediatamente.
3. Comparar de forma resistente a timing com as senhas real e falsa obtidas do Vault.
4. Para a senha falsa, responder de forma efêmera que não existe TOTP cadastrado.
5. Para qualquer outro valor incorreto, responder de forma efêmera que a senha é inválida.
6. Para a senha real, listar somente entradas com TOTP, aplicar o filtro e ordenar alfabeticamente antes da paginação.
7. Exibir botões inline paginados, navegação e cancelamento.
8. Ao escolher, remover a lista e enviar o código sozinho em uma mensagem copiável.
9. Enviar a expiração em outra mensagem.
10. Apagar ambas cinco segundos após o código expirar e encerrar a sessão.

Qualquer mensagem inesperada durante o fluxo cancela a sessão, remove as mensagens relacionadas e envia aviso efêmero de cancelamento. Um novo `/totp` substitui a sessão anterior.

## Filtro

Comparar sem diferenciar maiúsculas e minúsculas sobre o caminho completo da entrada. Tratar `*` como qualquer sequência. Um filtro `MSN*Rodrigo` encontra caminhos que contenham `MSN`, seguido posteriormente por `Rodrigo`. Não exigir que o usuário conheça o caminho exato.

## Paginação e callbacks

Não tentar colocar centenas de entradas em um único teclado. Paginar e usar callback curto com token de sessão não previsível e índice da entrada. Validar bot, chat, owner, token, fase e expiração em cada callback. Remover o teclado anterior ao mudar de página ou finalizar.

## Mensagens efêmeras

Manter solicitação de senha, listas, erros, avisos e código fora do histórico ao fim do fluxo. Proteger conteúdo das mensagens auxiliares; não proteger a mensagem do código para permitir cópia. O Telegram não oferece garantia criptográfica de efemeridade: exclusão reduz persistência do histórico, mas não impede notificações, screenshots, clientes modificados ou captura durante a validade.

## Período

Usar `totp.period_seconds`, padrão 30 segundos, coerente com as entradas. Calcular o restante no instante em que o código é devolvido e agendar exclusão para `remaining + 5 segundos`.

## Configuração

O perfil TOTP referencia duas entradas independentes no Vault:

```ini
[totp]
real_password_entry = caminho/da/senha-real
fake_password_entry = caminho/da/senha-falsa
```

Os nomes das entradas podem conter `:`; não inferir separação por espaços. Nunca armazenar os valores dessas senhas no perfil ou no JSON.

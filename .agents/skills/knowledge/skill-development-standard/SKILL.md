---
name: skill-development-standard
description: Padrão interno para planejar, criar, implementar, testar e documentar skills de integração e skills de conhecimento, incluindo estrutura de pastas, nomenclatura, perfis INI, configuração externa, integração segura com KeePassVault, contratos JSON, testes e integração posterior com o Akuma.
---

# Padrão de Desenvolvimento de Skills

Use este conhecimento ao criar ou revisar qualquer nova skill de integração, skill operacional ou skill de conhecimento pertencente ao ecossistema Akuma.

## Tipos de skill

Distinguir dois tipos principais de projeto:

- Skills de integração: projetos autônomos fora do Akuma, normalmente mantidos diretamente em `C:\x`.
- Skills de conhecimento: skills mantidas diretamente dentro de `C:\x\Akuma\.agents\skills\knowledge`.

Uma skill de integração deve funcionar isoladamente e não pode depender estruturalmente do projeto Akuma. A integração com o Akuma ocorre somente depois que o projeto externo estiver implementado, testado e validado.

Uma skill de conhecimento pertence ao Akuma e deve ser criada diretamente dentro da árvore de skills de conhecimento, sem criar um projeto externo separado.

## Localização e nomenclatura

Projetos autônomos de integração devem utilizar o prefixo `skill` no nome da pasta, seguido pelo nome da integração em PascalCase:

```text
C:\x\skillGoogle
C:\x\skillNotion
C:\x\skillTodoist
C:\x\skillCloudflare
C:\x\skillForwardEmail
```

Cada projeto deve ser um repositório Git independente.

Skills instaladas diretamente no Akuma devem utilizar nome normalizado em minúsculas, com hífens quando necessário:

```text
C:\x\Akuma\.agents\skills\knowledge\bis2
C:\x\Akuma\.agents\skills\knowledge\turing
C:\x\Akuma\.agents\skills\knowledge\skill-development-standard
```

Não misturar o prefixo `skill` dos projetos externos com a nomenclatura interna das skills descobertas pelo Akuma sem uma decisão explícita.

## Estrutura padrão de uma skill de integração

A estrutura mínima recomendada é:

```text
skillNome/
├── agents/
│   └── openai.yaml
├── configs/
│   └── nome.example.ini
├── references/
│   ├── api-contracts.md
│   ├── configuration.md
│   └── keepass-provider.md
├── scripts/
│   ├── nome.py
│   ├── __init__.py
│   └── test_nome.py
├── .gitignore
└── SKILL.md
```

Usar apenas os diretórios realmente necessários. Não criar README, changelog, guia de instalação ou documentação auxiliar que duplique o conteúdo da skill.

A estrutura mínima de uma skill de conhecimento pode conter somente:

```text
nome-da-skill/
├── agents/
│   └── openai.yaml
├── references/
│   └── arquivos-de-conhecimento.md
└── SKILL.md
```

Criar `scripts/`, `configs/` ou `assets/` somente quando forem necessários para o funcionamento da skill.

## Arquivo SKILL.md

Todo `SKILL.md` deve conter frontmatter YAML com somente:

```yaml
---
name: nome-da-skill
description: Descrição clara do que a skill faz e dos contextos que devem ativá-la.
---
```

O campo `description` deve explicar a capacidade e os gatilhos de uso. Não depender de uma seção posterior “Quando usar”, porque o corpo somente será carregado depois que a skill for ativada.

O corpo deve conter instruções operacionais objetivas, referências para arquivos complementares e regras de segurança. Manter o corpo preferencialmente abaixo de 500 linhas.

Escrever arquivos Markdown em UTF-8. Não quebrar linhas no meio de frases ou parágrafos; usar quebras somente para separar parágrafos, listas e blocos.

## Arquivo agents/openai.yaml

Quando a skill tiver interface de agente, criar:

```yaml
interface:
  display_name: "Nome amigável"
  short_description: "Descrição curta da capacidade"
  default_prompt: "Instrução padrão para usar a skill."
```

Os valores devem ser coerentes com o `SKILL.md`.

## Perfis de configuração

Skills de integração devem receber explicitamente o perfil do projeto hospedeiro por argumento:

```text
python scripts/nome.py --config C:\caminho\perfil.ini
```

O perfil real deve ficar em `configs/` e ser ignorado pelo Git. Versionar somente um modelo sem credenciais, normalmente:

```text
configs/nome.example.ini
```

O perfil deve separar:

- configuração funcional da API;
- endpoint base;
- domínio, zona, host ou ambiente;
- timeout;
- retries;
- paginação;
- referência do provedor KeePassVault.

Nunca armazenar no INI:

- senha;
- API token;
- API key;
- client secret;
- refresh token;
- chave privada;
- senha de mailbox ou alias;
- conteúdo sensível.

O wrapper deve validar o perfil antes de executar a operação.

## Integração com KeePassVault

Toda credencial deve ser obtida exclusivamente através da skill ou projeto `skillKeePassVault`.

A integração deve utilizar o wrapper externo:

```text
C:\x\skillKeePassVault\scripts\keepass_vault.py
```

O perfil hospedeiro deve informar:

```ini
[vault]
command = python
script = C:\caminho\skillKeePassVault\scripts\keepass_vault.py
config = C:\caminho\keepass-profile.ini
entry_path = Grupo/Entrada
field = password
auth_json = {"mode":"windows_credential_manager","target":"Akuma/KeePassXC/KeeVault"}
```

O wrapper deve chamar o KeePassVault como subprocesso, enviar uma requisição JSON com `version: 1` e realizar uma operação `read`.

Exemplo conceitual:

```json
{
  "version": 1,
  "operation": "read",
  "entry": {
    "path": "APIs/Servico"
  },
  "field": "password",
  "auth": {
    "mode": "windows_credential_manager",
    "target": "Akuma/KeePassXC/KeeVault"
  }
}
```

O segredo deve permanecer somente em memória.

Nunca:

- passar segredo em argumento de linha de comando;
- escrever segredo no arquivo de perfil;
- imprimir requisição ou resposta bruta do KeePassVault;
- registrar segredo em logs;
- incluir credencial em exceções;
- copiar segredo para clipboard;
- retornar segredo na resposta da skill, salvo quando a operação explicitamente exigir esse resultado e isso estiver documentado.

## Autenticação

Quando a integração exigir token, utilizar exclusivamente token.

Não implementar OAuth, refresh token, client ID, client secret, callback HTTP ou autorização interativa quando o contrato da skill for token-only.

A forma de transporte deve seguir a API do serviço:

- Bearer Token: `Authorization: Bearer <token>`;
- Basic token-only: token como usuário e senha vazia;
- outro mecanismo de token documentado oficialmente pelo provedor.

O tipo de token e as permissões mínimas devem ser documentados em `references/configuration.md` ou `references/api-contracts.md`.

## Wrapper e contrato JSON

O wrapper deve:

- receber `--config`;
- ler uma requisição JSON pelo `stdin`;
- exigir `version: 1`;
- aceitar somente operações registradas;
- retornar uma única resposta JSON pelo `stdout`;
- enviar diagnósticos não sensíveis para `stderr`;
- retornar erros estruturados;
- não aceitar endpoint ou método arbitrário sem justificativa explícita.

Formato de sucesso:

```json
{
  "version": 1,
  "ok": true,
  "operation": "resource.list",
  "data": {}
}
```

Formato de erro:

```json
{
  "version": 1,
  "ok": false,
  "error": {
    "code": "invalid_request",
    "message": "Descrição segura do erro"
  }
}
```

Não repassar respostas brutas do provedor quando elas puderem conter tokens, senhas, cabeçalhos ou outros segredos.

## Operações destrutivas

Operações de criação, alteração, exclusão, sobrescrita ou rotação de credencial devem ser registradas no contrato da API.

Operações destrutivas devem exigir:

```json
{
  "confirm": true
}
```

A skill não deve alterar silenciosamente IDs, filtros, datas, nomes, destinatários ou payloads.

Para operações de alto risco, documentar explicitamente:

- o que será alterado;
- se há perda de dados;
- se há impacto de disponibilidade;
- se a operação é reversível;
- quais parâmetros exigem atenção.

## API e referências

Manter detalhes extensos em `references/`, deixando o `SKILL.md` como guia principal.

Uma skill de integração normalmente deve possuir:

```text
references/api-contracts.md
references/configuration.md
references/keepass-provider.md
```

`api-contracts.md` deve documentar operações, parâmetros, paginação, respostas e efeitos destrutivos.

`configuration.md` deve documentar o formato do perfil, valores obrigatórios e exemplos sem segredos.

`keepass-provider.md` deve documentar como o wrapper chama o KeePassVault e como os segredos são protegidos.

Para APIs externas, consultar preferencialmente documentação oficial e registrar limitações ou endpoints não implementados.

## Testes

Toda skill de integração deve possuir testes automatizados em `scripts/test_nome.py`.

Os testes devem cobrir pelo menos:

- carregamento e validação do perfil;
- rejeição de endpoint inválido;
- rejeição de versão diferente de `1`;
- leitura da credencial pelo KeePassVault com subprocesso mockado;
- garantia de que o segredo não aparece em argumentos ou representação da chamada;
- autenticação correta;
- roteamento das operações;
- paginação, quando aplicável;
- timeout e retry;
- erros HTTP;
- confirmação obrigatória para escrita;
- rejeição de configuração sem HTTPS, quando aplicável.

Executar os testes com o launcher Python disponível no ambiente:

```text
py -3 -m unittest discover -s scripts -p "test_*.py"
```

Também validar a compilação:

```text
py -3 -m compileall -q scripts
```

Não testar operações destrutivas reais sem autorização explícita e sem validar o alvo.

## Validação da skill

Antes de concluir:

1. Validar o frontmatter e o nome da skill.
2. Executar os testes automatizados.
3. Executar a compilação dos scripts.
4. Conferir que perfis reais e segredos estão ignorados pelo Git.
5. Conferir que não existem credenciais em arquivos versionados.
6. Revisar mensagens de erro e logs.
7. Conferir se a documentação corresponde ao comportamento implementado.
8. Conferir se a skill funciona sem depender do Akuma, quando for uma integração externa.
9. Inicializar o repositório Git quando for um projeto autônomo.
10. Integrar ao Akuma somente após a validação local e a disponibilização do repositório externo.

## Integração posterior com o Akuma

A implementação externa deve permanecer independente até que o repositório esteja pronto para ser consumido pelo Akuma.

A integração posterior pode incluir:

- cópia ou referência da skill;
- inclusão em `.agents/skills`;
- atualização do catálogo de skills;
- configuração de caminhos locais;
- instalação ou atualização de plugin;
- testes de descoberta pelo agente.

Não realizar essa integração durante a criação do projeto externo, salvo solicitação explícita.

## Checklist final

Antes de considerar uma skill concluída, verificar:

- [ ] O nome da pasta segue o padrão correto.
- [ ] O projeto externo usa o prefixo `skill`.
- [ ] A skill de conhecimento está dentro do projeto Akuma.
- [ ] Existe `SKILL.md` válido.
- [ ] Existe `agents/openai.yaml` quando necessário.
- [ ] Existe perfil `.example.ini`.
- [ ] O perfil real é ignorado pelo Git.
- [ ] O KeePassVault é chamado externamente.
- [ ] Não há credenciais versionadas.
- [ ] A autenticação segue o contrato definido.
- [ ] OAuth não foi introduzido em integrações token-only.
- [ ] As operações estão documentadas.
- [ ] As operações destrutivas exigem confirmação.
- [ ] Os testes automatizados passam.
- [ ] A compilação passa.
- [ ] A integração com o Akuma ainda não foi feita sem autorização.

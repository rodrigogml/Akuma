---
name: turing
description: Conhecimento e procedimentos seguros para investigar, operar e planejar alterações no servidor Turing, ambiente de produção. Use quando a tarefa envolver o Turing, seus serviços, bancos, infraestrutura, deploys, manutenção, pausas, reinícios ou qualquer diagnóstico que possa afetar recursos ou disponibilidade.
---

# Turing

## Introdução

O Turing é o servidor de produção da operação. Esta skill orienta investigações, diagnósticos e futuras operações sobre os serviços hospedados nele, preservando disponibilidade, rastreabilidade e controle explícito das mudanças.

Trate qualquer alteração no Turing como uma operação de produção. Não confunda acesso técnico disponível com autorização para modificar o ambiente.

## Regras operacionais

- Execute livremente comandos de leitura e investigação leves, desde que não alterem o servidor nem consumam recursos relevantes.
- Planeje antes de executar qualquer alteração em arquivos, configurações, serviços, banco de dados, permissões, rede, pacotes ou processos.
- Obtenha autorização explícita do usuário antes de aplicar uma alteração planejada.
- Obtenha autorização explícita e específica antes de pausar, reiniciar, parar, iniciar, desabilitar ou interromper qualquer serviço.
- Não trate uma autorização para investigar como autorização para corrigir, reiniciar ou alterar.
- Não execute consultas extensas, dumps completos, exportações volumosas, varreduras amplas ou backups sem limitar consumo, estimar impacto e obter autorização quando houver risco operacional.
- Preserve evidências antes de qualquer alteração: estado atual, comandos efetivos, arquivos envolvidos, resultados e horário.
- Defina previamente impacto esperado, janela de execução, pré-condições, plano de rollback e critério de sucesso.
- Monitore o resultado da operação e confirme a recuperação do serviço antes de encerrar.
- Nunca exponha senhas, tokens, chaves privadas ou conteúdo sensível em comandos, logs ou respostas.
- Use as skills de integração apropriadas para SSH, MySQL, KeePassVault e demais sistemas; não invente credenciais nem caminhos.

## Classificação de solicitações

### Investigação livre

Inclui leitura de status, processos, serviços, espaço em disco, configurações não sensíveis, logs limitados e verificações pontuais de conectividade. Mantenha limites de tempo, quantidade de linhas e consumo de recursos.

### Investigação planejada

Inclui leitura de bancos extensos, análise de logs volumosos, dumps, backups, varreduras recursivas amplas e qualquer consulta com potencial de carga. Antes de executar, descreva escopo, limite de consumo, duração estimada e impacto esperado.

### Alteração autorizada

Inclui editar ou excluir arquivos, modificar configurações, executar scripts de manutenção, alterar dados, instalar pacotes, aplicar deploys, alterar permissões e modificar regras de rede. Apresente o plano e aguarde autorização explícita antes da execução.

### Interrupção autorizada

Inclui qualquer parada, pausa, reinício, troca de processo, failover ou alteração que possa interromper usuários ou integrações. Exija autorização explícita para a interrupção, mesmo que ela faça parte de um procedimento técnico recomendado.

## Fluxo obrigatório para mudanças

1. Identificar o serviço, host, ambiente e escopo exatos.
2. Investigar o estado atual sem modificar o Turing.
3. Descrever causa, risco, impacto, dependências e alternativas.
4. Apresentar os comandos ou alterações pretendidos, incluindo rollback.
5. Solicitar autorização explícita para a mudança ou interrupção.
6. Executar somente o escopo autorizado.
7. Validar saúde, disponibilidade e comportamento do serviço.
8. Registrar resultado, alterações realizadas e pendências.

## Estrutura de conhecimento

Acrescente informações específicas em referências separadas por domínio, mantendo esta introdução como o índice das regras comuns:

- Serviços de aplicação: adicionar uma referência por serviço, com função, dependências, portas, processo, logs, health check e procedimento de manutenção.
- Bancos de dados: documentar instâncias, bases, limites de consulta, rotinas de backup, restauração e procedimentos autorizados.
- Infraestrutura: documentar sistema operacional, armazenamento, rede, acesso, monitoramento e dependências externas.
- Operação BIS2: adicionar somente após confirmar serviços, componentes e procedimentos reais do Turing.
- Deploy e rollback: documentar pré-condições, artefatos, janela, validações e recuperação.

Ao receber uma tarefa sobre um domínio ainda não documentado, investigue apenas o necessário para estruturar o conhecimento e não presuma procedimentos de produção.

## Apache e domínios

### Organização dos diretórios dos VirtualHosts

No Turing, os VirtualHosts que servem arquivos usam diretórios próprios sob `/srv/apache/vhosts/<domínio>/<raiz-do-site>`. O caminho `/var/www` não é o padrão efetivo das aplicações existentes. Quando a aplicação reside em outro local, o diretório público do VirtualHost deve ser um link simbólico dentro dessa estrutura, apontando para a versão publicada da aplicação. Para o Ganttist, o caminho estável será `/srv/apache/vhosts/ganttist.rodrigogml.eng.br/www`, apontando para o diretório público da versão ativa em `/opt`.

O Turing utiliza Apache como camada web de produção, com serviço ativo e atendimento nas portas HTTP e HTTPS. A operação é organizada por VirtualHosts habilitados, que associam domínios e subdomínios a aplicações, sites e serviços distintos.

### Regras operacionais dos domínios

- Todo acesso HTTP deve ser redirecionado para HTTPS, preservando o host, o caminho da URL e todos os parâmetros recebidos.
- Os domínios e subdomínios web utilizam o Cloudflare para gerenciamento DNS, com os registros A, AAAA e CNAME compatíveis mantidos como `Proxied` (proxy habilitado/orange cloud).
- Registros DNS que não suportam proxy HTTP, como MX e TXT, permanecem fora dessa regra.
- A conexão pública é intermediada pelo Cloudflare, mas o Apache do Turing deve continuar aceitando HTTPS na origem.
- O modo SSL/TLS esperado no Cloudflare é `Full`, pois ele mantém HTTPS entre Cloudflare e origem sem validar a autoridade emissora do certificado. Isso permite o uso de certificados self-signed.
- O modo `Full (strict)` não deve ser usado com esses certificados self-signed, pois exige validação do certificado da origem.
- Os certificados self-signed utilizados nos domínios seguem validade operacional aproximada de 20 anos e devem ser acompanhados quanto à expiração.
- Alterações em redirecionamentos, proxy DNS, modo SSL/TLS ou certificados devem identificar previamente os domínios afetados e exigir autorização explícita quando houver risco de indisponibilidade.

No levantamento realizado em 15/08/2026, foram identificados os seguintes domínios e subdomínios sob gestão do Apache:

- Sites e domínios principais: `assurity.com.br`, `barinela.com.br`, `barinella.com.br`, `biserp.com.br`, `eracers.com.br`, `fotosepegadas.com.br`, `laizagalvan.eng.br`, `laveli.com.br`, `laveliconstrutora.com.br`, `laveli.eng.br`, `laveliengenharia.com.br`, `laveliincorporadora.com.br`, `rinos.com.br`, `rodrigogml.eng.br`, `rogerio.adv.br` e `talori.com.br`.
- Aplicações e subdomínios: `app.rinos.com.br`, `b10.biserp.com.br`, `bingo.rodrigogml.eng.br`, `bis10.biserp.com.br`, `bis2.biserp.com.br`, `intra.barinella.com.br`, `jarvis.rodrigogml.eng.br`, `mysteryrealms.rodrigogml.eng.br`, `tpanel.rodrigogml.eng.br`, `turing.rodrigogml.eng.br`, `wiki.biserp.com.br` e `wiki.rodrigogml.eng.br`.
- Aliases públicos `www` foram identificados para parte dos domínios principais e devem ser tratados como aliases dos respectivos sites, não como aplicações independentes.

Componentes relacionados encontrados ativos no levantamento: PHP-FPM 8.4 e MySQL. A relação acima é um índice operacional; não reproduza nesta skill caminhos, diretivas, certificados, regras de proxy ou demais detalhes que devem continuar sendo consultados diretamente no servidor.

Ao investigar o Apache, mantenha a distinção entre VirtualHosts habilitados, arquivos disponíveis e serviços auxiliares. Antes de qualquer alteração, identifique o domínio afetado, a aplicação associada, o impacto esperado e a necessidade de interrupção.

## Sistemas BIS2 e BIS10

O BIS2 e o BIS10 são aplicações corporativas Spring + Vaadin executadas em instâncias WildFly independentes instaladas sob `/opt`. Cada aplicação possui seu próprio serviço do sistema operacional e não depende do serviço WildFly genérico.

- `bis2.biserp.com.br` publica o BIS2.
- `bis10.biserp.com.br` e o alias `b10.biserp.com.br` publicam o BIS10.

O Apache atua como camada pública HTTPS e proxy reverso, encaminhando as requisições dos subdomínios para a instância WildFly correspondente. O WildFly fornece o ambiente de execução das aplicações, enquanto os serviços específicos BIS2 e BIS10 controlam sua inicialização e supervisão no sistema operacional.

Esta seção é um índice operacional. Para investigar deploys, portas, artefatos, logs ou configurações, consulte diretamente o Turing e não replique esses detalhes nesta skill.

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

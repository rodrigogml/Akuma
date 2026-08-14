---
name: bis2-knowledge
description: O BIS2 é um pequeno ERP utilizado para controlar vendas, documentos fiscais (NFe, NFCe, etc), convênios de consumo de clientes, cadastro de produtos, etc. Utilizado por algumas empresas. Use this skill when information about BIS2 system is needed. 
---

# BIS2

O BIS2 é um pequeno ERP utilizado para controlar vendas, documentos fiscais (NFe, NFCe, etc), convênios de consumo de clientes, cadastro de produtos, etc. Utilizado por algumas empresas.

## Instruções Gerais
- Sempre dê preferência por editar os valores do sistema pela skill BIS2CMD ao invés de alteração direta no banco de dados para não pular a validação do sistema. Na ausência de comandos para tarefa necessária, comunique o usuário, proponha melhoria na skill, mas peça permissão para realizar as alterações diretamente no banco nesses casos, sempre exibindo uma linha para cada alteração proposta, no formato:
	- "[tabela].[coluna]: [valor atual] -> [novo valor proposto]", quando alterações no banco de dados.
	- "[objeto].[propriedade[.subproriedades]]: [valor atual] -> [novo valor proposto]", quando alterações no objeto através da skill BIS2CMD.
- Alterações diretas no banco em itens ou códigos de itens devem atualizar também item_item.lastchange e, quando aplicável, item_itemcodes.lastchange para a data/hora atual. Essas colunas são usadas para detectar alterações e propagar o cadastro aos demais sistemas; alterar somente os dados fiscais não garante a sincronização.


## Banco de Dados
- Para acessr o banco de dados de desenvolvimento/produção, leia `PRIVATE-NOTES` para conhecer os ambientes e acessos configurados.

## Código Fonte
- O código fonte do sistema pode ser encontrado lendo `PRIVATE-NOTES.md` na raiz do repositório. O repositório no github é `git@github.com:rodrigogml/bis.git`.
	- O sistema é um mono repositório, dividido em vários projetos menores que se completam para montar o EAR, ou como aplicações auxiliares.
	- Dentro do repositório também há projetos relacionados ao RFW (Rodrigo's FrameWork), na versão que é utilizado no BIS2.

## Índice

- Para informações sobre Cupons Fiscais (NFCe), leia `references/cuponsFiscaisNFCe.md`.


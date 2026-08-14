# Cupons Fiscais - NFCe

# Instruções sobre Cupons Fiscais
- `SELLING`, `CANCELLING` e `SEFAZVALIDATING` — são status transitórios do documento fiscal e devem durar apenas alguns minutos. Documentos encontrados com esse status cuja data de emissão seja maior que 1 ou 2 dias são cupons que falharam em erro e precisam ser tratados.
- `STORED`, `SOLD`, `CANCELED`, `ERROR`, `ERROR_SYNC`, `VOID` e `SEFAZPROBLEM` — São status 'finais' do cupom.
- `SEFAZOFFLINE` - Status indica que o cupom está na fila para ser enviado para a SEFAZ.
- Sempre que apresentar dados de um documento fiscal, apresente sua identificação com o valor de `id`, `serie` e `number` no formato: "([id]) [serie]/[number]", se algum valor não estiver disponível deixe claro com o símbolo "-". Exemplo: "(156478) -/-".

## Banco de Dados

### Visão geral

No modelo fiscal atual, um cupom NFC-e é representado por um registro em `fiscal_docfiscal`, normalmente identificado por `type = 'NFCe'`. Essa tabela é o cabeçalho do documento e concentra a identificação, o emitente, o destinatário, os totais, os impostos resumidos, o status e os dados de emissão.

As informações detalhadas ficam em tabelas filhas relacionadas pelo campo `idfiscal_docfiscal`:

```text
fiscal_docfiscal
├── fiscal_docfiscalitem     (itens vendidos)
├── fiscal_docfiscalpayment  (pagamentos)
├── fiscal_docfiscalnfce     (parâmetros específicos da NFC-e)
└── fiscal_docfiscalfile     (arquivos associados, especialmente XML)
```

As relações filhas possuem chave estrangeira para `fiscal_docfiscal.id` e exclusão em cascata. Portanto, o `id` do documento fiscal é a chave de navegação principal.

### `fiscal_docfiscal` — cabeçalho do documento fiscal

Escopo: um registro representa o documento fiscal emitido ou em processamento.
Para NFC-e `type = 'NFCe'`.

Principais grupos de informações:

- Identidade e origem: `id`, `type`, `issuingmethod`, `idcore_company`, `idpdv_checkouts`, `idpdvlocal`, `deviceID` e `subDeviceId`.
- Numeração e identificação fiscal: `number`, `serie`, `natop`, `chave` e `emission`.
- Participantes: `emitCpfCnpj`, `emitName`, `destCpfCnpj` e `destName`.
- Controle: `operator`, `status`, `printed`, `emailed`, `email`, `validationStatus` e `validationErrorCode`.
- Totais: `totaldiscount`, `totalitem`, `total`, `moneychange` e `otherIncidentalExpensesValue`.
- Impostos e estimativas: `taxfed`, `taxest`, `taxmun`, `taxsource`, `taxsourcekey`, `pisValue`, `cofinsValue`, `pisStValue`, `cofinsStValue` e `icmsValue`.
- Informações especiais: `charityCNPJ` e `usedtokens`.

`number` e `serie` identificam a numeração dentro do estabelecimento, enquanto `chave` é a chave de acesso de 44 caracteres quando já disponível.
O campo `status` indica o estado operacional do documento; `validationStatus` e `validationErrorCode` registram a validação adicional realizada pelo sistema.

Relações diretas:
- `idcore_company` → `core_company.id`: empresa emissora.
- `idpdv_checkouts` → `pdv_checkouts.id`: caixa/checkout de origem, quando informado.
- `fiscal_docfiscal.id` é referenciado pelas quatro tabelas filhas descritas abaixo.

### `fiscal_docfiscalitem` — itens do documento

Escopo: uma linha de produto ou serviço que compõe o documento fiscal. A tabela armazena um retrato fiscal e comercial do item no momento da emissão; não se deve depender apenas do cadastro atual do produto para reconstruir um documento antigo.

Campos principais:
- Vínculo e ordenação: `idfiscal_docfiscal`, `iditem_item`, `idpdvlocal`, `orderindex` e `status`.
- Identificação exibida: `code`, `displayline`, `unit`, `measureunit`, `quantity`, `qcfactor`, `price`, `cost` e `total`.
- Classificação fiscal: `ncm`, `extipi`, `cest`, `cbenef`, `cfop`, `cst`, `icmstype`, `icmstaxratio`, `itemproductor` e `itemorigin`.
- Acréscimos e descontos: `discount`, `freight`, `insurance` e `otherExpenses`.
- PIS/COFINS e ICMS: `pisCofinsCST`, `pisCofinsNatRec`, `pisCofinsNatRecTable`, `pisRatio`, `cofinsRatio`, `icmsValue`, `pisValue`, `cofinsValue`, `taxfed`, `taxest` e `taxmun`.
- Reforma tributária: campos `ibsCbsCST`, `ibsCbsCClassTrib`, `ibsCbsTaxRatioType`, alíquotas `ibs*`/`cbs*` e valores `ibsUFValue`, `ibsMunValue` e `cbsValue`.
- Operação interna: `token`, `note`, `deviceId`, `backupICMSType` e `backupICMSTaxRatio`.

`iditem_item` → `item_item.id` é uma associação ao cadastro de itens. A existência dessa associação não substitui os campos fiscais gravados na própria linha do documento.

### `fiscal_docfiscalpayment` — pagamentos

Escopo: uma forma de pagamento utilizada no documento. Um NFC-e pode possuir vários pagamentos, por isso a consulta deve tratar essa tabela como relação 1:N.

Campos principais:
- Identificação e valor: `idfiscal_docfiscal`, `idPDVLocal`, `name`, `type`, `value`, `status`, `acceptexchange`, `changetype` e `changevalue`.
- TEF: `tefmodel`, identificadores de transação, NSU, chaves de autorização, controles, datas, terminal, adquirente e dados da bandeira.
- Comprovantes TEF: campos `tef_receipt*` e respectivos campos de reversão.
- Convênio e crédito: `contract_type`, `contract_code`, `contract_id`, `contract_name` e `tchange_uid`.
- Apresentação: `iconpath` e `tef_ReceiptToPrint`.

Os campos TEF podem conter comprovantes e identificadores operacionais. Evite exibi-los em consultas genéricas ou documentar valores reais; para entender a estrutura, consulte somente metadados ou dados anonimizados.

### `fiscal_docfiscalnfce` — dados específicos da NFC-e

Escopo: parâmetros complementares da NFC-e que não pertencem ao cabeçalho genérico. Contém `idfiscal_docfiscal`, `idpdvlocal`, `tpemis`, `tpamb`, `dhcont` e `xjust`.

- `tpemis`: forma de emissão, incluindo situações como emissão normal ou em contingência, conforme os valores usados pelo sistema.
- `tpamb`: ambiente fiscal, como produção ou homologação.
- `dhcont` e `xjust`: data/hora e justificativa de contingência, quando aplicáveis.

O banco possui chave estrangeira para o documento, mas não impõe unicidade em `idfiscal_docfiscal`; portanto, não assumir uma relação 1:1 apenas pelo modelo físico. Validar a cardinalidade dos dados antes de usar `JOIN` que possa duplicar o documento.

### `fiscal_docfiscalfile` — arquivos associados

Escopo: associa um documento fiscal a arquivos armazenados no sistema. Contém `idfiscal_docfiscal`, `type` e `idcore_systemfilesxml`.
- `type` identifica a função do arquivo no ciclo do documento.
- `idcore_systemfilesxml` → `core_systemfiles.id` aponta para o armazenamento central do arquivo/XML.

Essa tabela não guarda necessariamente o conteúdo XML diretamente; ela guarda o vínculo com o registro de arquivo. Para localizar o XML, navegar de `fiscal_docfiscalfile` para `core_systemfiles`.

### Tabelas relacionadas ao contexto da venda

- `core_company`: empresa emissora, alcançada por `fiscal_docfiscal.idcore_company`.
- `pdv_checkouts`: caixa/checkout de origem, alcançado por `fiscal_docfiscal.idpdv_checkouts`.
- `item_item`: cadastro do item, alcançado por `fiscal_docfiscalitem.iditem_item`.
- `core_systemfiles`: armazenamento dos arquivos vinculados, alcançado por `fiscal_docfiscalfile.idcore_systemfilesxml`.
- `pdv_creditcupom`: cupons de crédito/contra-vales. Migrações recentes permitem vínculos opcionais com `fiscal_docfiscal` e `fiscal_docfiscalpayment`.
- `pdv_contractstatement`: extratos de convênio podem referenciar `fiscal_docfiscal` por `idfiscal_docfiscal`.

### Modelo legado `pdv_cupom*`

Modelo era utilizado para salvar cupons do tipo SAT, não alterar ou investigar se nada sobre SAT for mencionado. Nâo há nenhuma relação com cupons NFCe.

### Navegação recomendada

Para localizar um documento e seus componentes, usar o identificador do documento ou a chave de acesso e consultar cada relação separadamente:

```sql
-- Cabeçalho
SELECT * FROM fiscal_docfiscal WHERE id = :id OR chave = :chave;

-- Itens
SELECT * FROM fiscal_docfiscalitem WHERE idfiscal_docfiscal = :id ORDER BY orderindex, id;

-- Pagamentos
SELECT * FROM fiscal_docfiscalpayment WHERE idfiscal_docfiscal = :id ORDER BY id;

-- Dados específicos da NFC-e
SELECT * FROM fiscal_docfiscalnfce WHERE idfiscal_docfiscal = :id ORDER BY id;

-- Arquivos/XML vinculados
SELECT f.*, sf.* FROM fiscal_docfiscalfile f JOIN core_systemfiles sf ON sf.id = f.idcore_systemfilesxml WHERE f.idfiscal_docfiscal = :id ORDER BY f.id;
```

Para relatórios, substituir `SELECT *` por colunas explícitas e evitar selecionar comprovantes TEF, e-mails, documentos de pessoas ou conteúdo de arquivos sem necessidade. Ao combinar itens e pagamentos em um único `JOIN`, lembrar que ambas são relações 1:N e o produto cartesiano pode duplicar totais.

## Estrutura Objetos

Quando necessário investigar diretamente o repositório ou pasta com o código fonte descritos em `PRIVATE-NOTES.md`.

## Status do DocFiscal
A coluna de enumeração `fiscal_docfiscal.status` indica a situação do documento atual, sendo os valores:
- `SELLING` — venda em montagem, com itens sendo registrados.
- `CANCELLING` — processo de cancelamento em andamento.
- `SEFAZVALIDATING` — venda concluída, aguardando validação ou retorno da SEFAZ.
- `STORED` — venda guardada em um token para continuar ou finalizar posteriormente.
- `SOLD` — venda concluída e documento emitido com sucesso.
- `CANCELED` — venda/documento cancelado.
- `ERROR` — finalização encerrada com erro do sistema.
- `ERROR_SYNC` — falta de sincronização; o sistema não tem certeza se o cupom foi registrado. É analisado posteriormente pelo usuário, não deve ser tratado como erro atual somente pela existência do status. Ele pode representar o cupom original de uma ocorrência já revisada ou regularizada. Só deve ser relatado como pendência quando houver evidência adicional de problema ainda não tratado.
- `VOID` — numeração/documento inutilizado.
- `SEFAZPROBLEM` — envio rejeitado ou com problema na SEFAZ. É necessário corrigir antes de tentar novamente.
- `SEFAZOFFLINE` — NFC-e emitida em contingência offline, ainda aguardando envio à SEFAZ.




## Resolução de Problemas

- Durante a solução dos problema a seguir, ao exibir as informações para o usuário é importante que os valores não sejam normalizados, tratados ou omitidos para que ele tenha todas as informações para uma tomada de decisão adequada.

### Cupons com problema de envio para SEFAZ - Status do cupom = SEFAZPROBLEM

Um cupom recebe o status `SEFAZPROBLEM` quando ocorre algum problema durante o envio ou processamento pela SEFAZ. As causas podem incluir falha de conexão ou comunicação com os serviços fiscais, indisponibilidade do ambiente da SEFAZ ou rejeição provocada pelo preenchimento dos dados fiscais.

Esse status identifica que o fluxo de emissão não foi concluído normalmente, mas não determina sozinho a causa do problema.

A resolução do problema se dá no seguinte fluxo:
1. Investigação do problema:
	- Lêr a coluna `fiscal_docfiscal.xJust` do cabeçalho do cupom. Este campo trás a informação da rejeição da SEFAZ quando o problema foi rejeição, ou mensagem do próprio sistema quando a falha ocorre antes mesmo do envio para a SEFAZ.
	- Se o erro relacionar algum item/produto do documento é preciso tomar cuidado pois o orderindex do BIS2 começa a contar do '0', enquanto que a SEFAZ começa a contagem do item a partir do '1'.
	- Com base no erro lido, investigar os campos relacionados ao erro do documento e extender a busca dos mesmos campos do cadastro de itens quando aplicável.

2. Correção dos Dados falhos, se houver:
	- Caso o problema relatado seja só falha de comunicação não há nada a para corrigir nos dados do cupom, apenas avançar para o próximo passo.
	- Caso o problema envolva erros de preenchimento dos dados do cupom:
		- Analisar os dados da investigação e entender a rejeição;
		- Analisar os dados do cadastro de itens quando aplicável para entender o porque o cupom foi mal preenchido;
	- Apresentar para o usuário a descrição do problema com todas as informações encontradas e a explicação tanto do motivo da rejeição, quando do motivo que causam a má formação do documento.
	- Apresentar a proposta de solução, mostrando exatamente que campos precisam ser alterados.
	- Só realiza a alteração após a explicita autorização do usuário sobre as alterações propostas.

3. Correção do XML da NFCe
	- Mesmo após os dados do documento corrigidos no banco de dados é preciso chamar o comando `fixNFCe` que é responsável por:
		- reescrever o XML a ser enviado para a SEFAZ com os dados encontrados no banco de dados (incluindo os que foram corrigidos no passo anterior);
		- troca o status de `SEFAZPROBLEM` para `SEFAZOFFLINE`.

4. Envio da NFCe para a SEFAZ
	- Chamar o comando `nfceSendOffline` envia o documento para a SEFAZ, mas só acenta o reenvio quando o status é `SEFAZOFFLINE`;
	
5. Verificação do Sucesso
	- O comando `nfceSendOffline` não necessariamente dá erro se o cupom for rejeitado, ele falha se houver falha no envio, não no aceite pela SEFAZ;
	- Após o envio é preciso consultas o status do cupom, é considerado corrigido se seu status passar para `SOLD`, caso volte para `SEFAZPROBLEM` indica que há um novo erro e devemos recomeçar o processamento.

#### Roteiro de Solução para Problemas já Diagnosticados:
- Falta de cBENEF para o CST no item do cupom:
	- Analisar a falta do cBENEF nos itens do cupom problemático;
	- Buscar o cBENEF no cadastro do item relacionado, verificando se já foi corrigido ou também está ausente;
	- Procurar cBENEF tem itens semelhantes do sistema, e validar questionar com busca na internet considerando o estado de atuação da empresa;

	
	
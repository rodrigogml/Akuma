# Ganttist

## Aplicação

O Ganttist é uma aplicação Laravel 12/PHP. O código-fonte oficial está no repositório `git@github.com:rodrigogml/Ganttist.git`. A versão inicial instalada no Turing foi a tag `v1.0.0`, correspondente ao commit `5d63d0acf006a1d3cec6392f5a68bf54d82196ee`.

## Checkout no Turing

O checkout de produção está em `/opt/Ganttist`. A instalação inicial foi feita por clone HTTPS do repositório com a tag `v1.0.0`, em 16/08/2026. O diretório ficou com proprietário `root:root`, modo `755` e HEAD destacado na tag, sem branch de trabalho.

O checkout contém `public/index.php` e `composer.json`, confirmando a estrutura esperada da aplicação. A atualização deve continuar sendo feita por uma nova tag explicitamente escolhida, mantendo o código de produção fixado em uma versão imutável.

## Publicação web

O DocumentRoot esperado para o Apache é `/opt/Ganttist/public`. O diretório raiz do projeto não deve ser publicado diretamente, pois contém configurações, dependências e dados que não fazem parte da superfície web.

## Estado da instalação

Esta etapa apenas publicou o código em `/opt/Ganttist`. Apache, VirtualHost, TLS, PHP-FPM, banco de dados, filas, scheduler, variáveis de ambiente e monitoramento ainda não foram configurados para o Ganttist. Não considerar a aplicação disponível em produção até essas etapas serem executadas e validadas.

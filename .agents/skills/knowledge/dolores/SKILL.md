---
name: dolores
description: Conhecimento sobre Dolores, o Raspberry Pi que será o servidor local da automação do apartamento e hospedará o openHAB. Use quando a tarefa envolver Dolores, seu acesso SSH, sistema operacional, rede, openHAB, automações, serviços, armazenamento, backups ou manutenção.
---

# Dolores

Dolores é o Raspberry Pi destinado a atuar como servidor local da automação do apartamento. O serviço principal planejado é o openHAB.

## Objetivos do servidor

- Hospedar o openHAB como núcleo local da automação do apartamento.
- Manter as automações funcionando localmente mesmo durante indisponibilidade da Internet.
- Centralizar integrações, regras, estados e histórico necessários à automação residencial.
- Priorizar estabilidade, baixo consumo, manutenção simples e recuperação rápida.
- Manter observabilidade, backups externos e procedimentos documentados antes de ampliar o ambiente.

## Estado conhecido

- Host SSH: `192.168.68.100`.
- Usuário SSH: `rodrigogml`.
- Porta SSH: `22`.
- Autenticação: senha armazenada no KeePassVault; nunca reproduzir a senha em respostas, comandos ou arquivos versionados.
- Entrada SSH no Vault: `Servidores/Dolores:SSH:rodrigogml`.
- O acesso local usa o perfil SSH `ssh_Dolores`.
- Modelo: Raspberry Pi 3 Model B Rev 1.2.
- CPU: ARM Cortex-A53, 4 núcleos, até 1,2 GHz, arquitetura `aarch64`.
- Memória: aproximadamente 905 MiB de RAM e 904 MiB de zRAM.
- Armazenamento: cartão microSD de aproximadamente 64 GB; partição raiz ext4 com aproximadamente 53 GB livres no inventário realizado.
- Sistema operacional: Debian GNU/Linux 13 “trixie”.
- Kernel: `6.18.34+rpt-rpi-v8`.
- Temperatura observada: aproximadamente 48,3 °C.
- Interfaces de rede Ethernet e Wi-Fi ativas; Ethernet usa `192.168.68.100` e Wi-Fi usa `192.168.68.32`.
- O openHAB ainda não está registrado como serviço systemd.
- O Docker está inativo.

## Limitações e decisões operacionais

- Tratar 1 GB de RAM como o principal limite de capacidade; manter poucos serviços residentes e evitar workloads pesados, bancos volumosos, múltiplos containers e compilação local.
- Priorizar Ethernet para o tráfego do openHAB e da automação. Evitar manter duas interfaces ativas sem uma decisão explícita de roteamento, failover ou finalidade.
- Tratar o microSD como armazenamento de sistema, não como destino de escrita intensa. Limitar logs, retenções e históricos; avaliar armazenamento externo antes de aumentar a persistência do openHAB.
- Manter backups fora da Dolores, porque o microSD é um ponto único de falha e não oferece redundância.
- Não considerar a zRAM como substituta de RAM física. Investigar consumo e pressão de memória antes de instalar novos serviços.
- Monitorar temperatura, alimentação e ventilação. Evitar operar continuamente próximo de limites térmicos ou com fonte subdimensionada.
- Planejar atualizações, mudanças de bindings, reinicializações e alterações de rede com validação e rollback, pois a máquina concentra a automação local.
- Não presumir que o openHAB está instalado até confirmar o método de instalação, a versão e os arquivos de configuração.

## Regras de operação

- Tratar Dolores como infraestrutura residencial; preservar disponibilidade dos serviços de automação.
- Antes de alterar sistema, rede, serviços, arquivos, pacotes ou configurações, investigar o estado atual e apresentar o plano, impacto, pré-condições, rollback e critério de sucesso.
- Solicitar autorização explícita antes de aplicar alterações ou reiniciar/parar serviços.
- Usar a skill de integração SSH e o perfil configurado; nunca colocar credenciais em comandos, logs ou documentação.
- Preferir comandos de leitura limitados para inventário e diagnóstico.

## Índice de conhecimento

À medida que o ambiente for investigado, adicionar referências separadas por domínio e manter este arquivo como índice:

- `references/infraestrutura.md`: hardware, sistema operacional, armazenamento, rede, hostname e acesso.
- `references/openhab.md`: versão, instalação, addons, arquivos de configuração, serviços, portas, persistência e backup.
- `references/automacao.md`: itens, regras, bindings, dispositivos e dependências da automação do apartamento.
- `references/operacao.md`: procedimentos de diagnóstico, atualização, backup, restauração e rollback.

Não presumir detalhes ainda não confirmados no Raspberry Pi.

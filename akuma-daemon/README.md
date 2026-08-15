# Akuma Daemon

O Akuma Daemon é o único processo do Akuma que deve ser registrado como serviço do sistema operacional. Ele atua como supervisor: inicia, monitora, reinicia, habilita, desabilita e encerra os serviços internos.

O primeiro serviço interno é o `task-scheduler`, responsável por tarefas, recorrências, execução de scripts e histórico. O SQLite desse serviço fica em `configs/daemon/services/task-scheduler/`; o endpoint e o estado do supervisor ficam em `configs/daemon/supervisor/`. Esses dados são privados e estão protegidos pelo `.gitignore` da raiz.

## Desenvolvimento

```powershell
py -3 -m pip install -e ".[test]"
py -3 -m pytest -q
```

Para executar o supervisor em foreground:

```powershell
py -3 -m akuma_daemon run
```

Em outro terminal, a CLI controla o supervisor e encaminha as operações ao serviço interno:

```powershell
py -3 -m akuma_daemon list-services
py -3 -m akuma_daemon add email-check python --arg=-m --arg=my_script --interval 3600
py -3 -m akuma_daemon list
py -3 -m akuma_daemon pause email-check
py -3 -m akuma_daemon resume email-check
py -3 -m akuma_daemon run-now email-check
py -3 -m akuma_daemon history email-check
py -3 -m akuma_daemon remove email-check
```

Comandos de ciclo de vida dos serviços internos:

```powershell
py -3 -m akuma_daemon start task-scheduler
py -3 -m akuma_daemon stop task-scheduler
py -3 -m akuma_daemon restart task-scheduler
py -3 -m akuma_daemon enable task-scheduler
py -3 -m akuma_daemon disable task-scheduler
py -3 -m akuma_daemon status task-scheduler
```

## Telegram Gateway

O Telegram Gateway é iniciado pelo supervisor como o serviço interno `telegram`. Copie os modelos de `examples/telegram/` para `configs/daemon/services/telegram/`, ajuste as referências do vault e crie um arquivo JSON por bot em `bots/`. O token é resolvido somente pelo wrapper KeePassVault em tempo de execução.

```powershell
py -3 -m akuma_daemon telegram bots
py -3 -m akuma_daemon telegram start-listener bot-principal
py -3 -m akuma_daemon telegram pair-request bot-principal
py -3 -m akuma_daemon telegram owners bot-principal
py -3 -m akuma_daemon telegram send bot-principal 123456789 "Mensagem de teste"
py -3 -m akuma_daemon telegram send bot-principal -100123456789 "Mensagem no tópico" --thread-id 42
```

O listener responde inicialmente a qualquer mensagem com `Não incomode o Akuma`. O comando `/pair 123456` (também aceito como `/painr` por compatibilidade) consome o PIN único emitido por `pair-request` e registra o remetente como owner do bot.

O adaptador de Windows Service usa o mesmo supervisor e requer `pywin32`:

```powershell
py -3 -m pip install -e ".[windows]"
py -3 -m akuma_daemon.windows_service install
```

# Akuma

Repositório de configuração e skills locais para uso com Codex.

## Skills

As skills do projeto ficam em `.agents/skills/`. Algumas podem ser submódulos Git; clone este repositório com:

```powershell
git clone --recurse-submodules git@github.com:rodrigogml/Akuma.git
```

Para inicializar submódulos após um clone já existente:

```powershell
git submodule update --init --recursive
```

## Configurações locais

Arquivos de configuração das skills ficam em `configs/` e nunca são versionados.

Use um destes formatos:

```text
configs/<nome-da-skill>.ini
configs/<nome-da-skill>_<perfil>.ini
```

O nome deve começar pelo nome da skill. Quando houver mais de um perfil, separe o identificador por `_`, por exemplo `keepass-vault_personal.ini` e `keepass-vault_work.ini`. Não armazene senhas, chaves ou arquivos KDBX no repositório.

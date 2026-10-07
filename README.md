# Netmonitor

Monitor de estabilidade da rede e da internet, com coleta contínua de latência, perda de pacotes, resolução DNS e velocidade de download/upload. Os resultados são armazenados em SQLite para investigar interrupções, degradação e mudanças de desempenho ao longo do tempo.

Pode ser executado em computadores, servidores e dispositivos de baixo consumo com as dependências necessárias. Alvos, interface de rede, intervalos, retenção e velocidades contratadas são configuráveis. O coletor funciona sem interface gráfica; o dashboard web será uma etapa posterior.

## Funcionalidades

- Probes ICMP a cada 2 segundos para o gateway e dois alvos públicos.
- Consultas DNS a cada 30 segundos pelo resolver do sistema e por um resolver público explícito.
- RTT médio/máximo, perda, disponibilidade observada, variação de RTT e cobertura da coleta.
- Incidentes por alvo, conectividade local, WAN e resolver DNS, com confirmação e recuperação configuráveis.
- Testes opcionais de download/upload em intervalos configuráveis, com comparação percentual com o plano contratado.
- Registro do IP público observado e comparação opcional com um endereço esperado.
- Histórico de 1 hora, 24 horas e 7 dias, com agregação para gráficos.
- Retenção de dados, migrações SQLite, lock contra duas instâncias e encerramento com flush.
- Execução automática por systemd e testes offline.

Erros operacionais e ausência de coleta são registrados separadamente de perda de pacotes. Falhas de DNS ou speedtest não alteram o resultado dos probes ICMP.

## Compatibilidade e requisitos

O coletor usa recursos de processos e locking disponíveis em Linux e macOS. A instalação automática como serviço usa systemd em distribuições Linux que o disponibilizam. Os comandos de instalação abaixo são exemplos para sistemas com APT; em outras distribuições, use o gerenciador de pacotes correspondente.

- Python 3.9 ou superior.
- `fping` para ICMP.
- `dnspython`, instalado em ambiente virtual, para DNS assíncrono.
- CLI oficial Speedtest by Ookla, quando o teste de banda estiver habilitado.
- Conexão de rede e máquina ligada, sem suspensão automática, para coleta contínua.

Ethernet é recomendada para observar a conexão cabeada. A capacidade da interface limita os testes de banda; gigabit não é requisito para medir estabilidade, e IP fixo não é requisito do projeto.

O pacote `sqlite3` do sistema fornece a ferramenta de inspeção; a persistência usa o módulo `sqlite3` da biblioteca padrão do Python.

## Instalação

Em distribuições com APT, como Debian, Ubuntu e Linux Mint, instale as dependências e crie o ambiente virtual. O caminho `~/netmonitor` é um exemplo; ajuste para a pasta onde o projeto foi instalado:

```bash
sudo apt update
sudo apt install python3 python3-venv sqlite3 fping curl ca-certificates

cd ~/netmonitor
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

No macOS, instale Python e `fping` pelo gerenciador de pacotes disponível; com Homebrew, `brew install python fping`. Crie o ambiente virtual e instale `requirements.txt` da mesma forma. O estado físico via `/sys/class/net` é opcional e pode ficar nulo fora do Linux.

Crie o ambiente virtual na própria máquina de destino e execute o coletor como usuário normal.

### Configuração local

Use `config.json` como base para coleta de estabilidade ou `config.notebook.json` como exemplo com testes de banda habilitados. O nome do segundo arquivo identifica um perfil de configuração, sem restringir seu uso a notebooks. O exemplo abaixo usa esse perfil e cria `config.local.json`, ignorado pelo Git:

```bash
cp -n config.notebook.json config.local.json
ip route get 1.1.1.1
nano config.local.json
```

No Linux, `ip route get` mostra a interface e a rota utilizada. Ajuste `interface` para a conexão que deseja medir, por exemplo `enp2s0`. O nome varia entre máquinas; no macOS, confira a rota com `route -n get default`. Confira também o gateway e os alvos da sua rede. Os endereços e velocidades dos perfis são exemplos, não requisitos.

| Configuração | Exemplo em `config.notebook.json` |
| --- | --- |
| Gateway | `10.10.10.254` |
| Alvos WAN | `1.1.1.1` e `8.8.8.8` |
| ICMP | Intervalo de 2 s; timeout por alvo de 700 ms; timeout global de 1,8 s |
| DNS | `example.com`, tipo A, a cada 30 s; timeout de 3 s |
| Banco | `data/monitor.db`, criado automaticamente |
| Retenção | 30 dias de amostras; 365 dias após encerramento de incidentes |
| Confirmação/recuperação | 3 falhas / 2 sucessos consecutivos |
| Speedtest | Habilitado; primeiro após 5 min; repetição a cada 6 h |
| Timeout/cooldown de speedtest | 180 s / 10 s |
| Plano contratado | 1000 Mb/s de download e upload |
| IP esperado | Nulo; comparação desativada até preencher `speedtest.expected_public_ip` |

`speedtest.plan_download_mbps` e `plan_upload_mbps` devem refletir o plano contratado, inclusive quando download e upload têm velocidades diferentes. `speedtest.ip_mode` aceita `static` ou `dynamic` e registra a característica do plano, sem configurar o IP da interface. Preencher `expected_public_ip` permite comparar com o IP observado. `server_id` pode fixar um servidor de teste; nulo permite seleção pelo CLI.

O arquivo `config.json` é um perfil sem teste automático de banda. `config.notebook.json` é o perfil com banda habilitada. Configurações antigas sem a seção `speedtest` continuam válidas e mantêm essa coleta desativada.

`--config` relativo é resolvido contra a pasta de `collector.py`. Caminhos de banco e binários relativos são resolvidos contra a pasta da configuração; nomes simples de binário usam o PATH. A execução não depende do diretório corrente.

### Speedtest by Ookla

Use o [CLI oficial da Ookla](https://speedtest-static-dev.speedtest.dev/apps/cli). O pacote Python `speedtest-cli` possui outro formato e não é compatível com este adaptador.

Confira a arquitetura:

```bash
uname -m
```

Para `x86_64`, instale o binário Linux publicado pela Ookla:

```bash
cd ~/netmonitor
download_dir=$(mktemp -d)
curl -fL https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-x86_64.tgz -o "$download_dir/speedtest.tgz"
tar -xzf "$download_dir/speedtest.tgz" -C "$download_dir"
mkdir -p bin
install -m 755 "$download_dir/speedtest" bin/speedtest
bin/speedtest --help
bin/speedtest
```

Para outra arquitetura ou sistema operacional, selecione a instalação correspondente na página oficial. Ajuste `speedtest.binary` para o caminho instalado; o perfil com banda usa `bin/speedtest` como exemplo. O diretório `bin/` é ignorado pelo Git.

A primeira execução manual pode solicitar aceitação de termos: leia e aceite se concordar, usando o mesmo usuário que executará o serviço. O coletor não aceita termos automaticamente. `bin/speedtest` também executa uma medição real.

Para coletar somente estabilidade, defina `speedtest.enabled` como `false`; a instalação da Ookla deixa de ser necessária.

## Uso

Validar ICMP como usuário normal, substituindo o gateway pelo endereço da sua rede:

```bash
fping -C 5 -q 10.10.10.254 1.1.1.1 8.8.8.8
```

Executar uma rodada de ICMP e DNS:

```bash
.venv/bin/python collector.py --config config.local.json --once
```

Incluir um teste de banda imediato:

```bash
.venv/bin/python collector.py --config config.local.json --once --speedtest
```

`--once` não executa speedtest automaticamente, mesmo com banda habilitada na configuração. `--speedtest` exige `--once`. Timeouts limitam a execução; velocidades de testes malsucedidos ficam nulas, nunca são convertidas em zero.

Iniciar coleta contínua e consultar a ajuda:

```bash
.venv/bin/python collector.py --config config.local.json
.venv/bin/python collector.py --help
```

Encerre a coleta com `Ctrl+C`. SIGTERM também interrompe tarefas, termina e aguarda subprocessos e grava eventos completos já enfileirados. Um probe cancelado não é registrado como perda. Uma segunda instância contra o mesmo banco é recusada.

Código de saída 0 indica execução concluída, inclusive com perdas ICMP ou falhas de resolução DNS. Código 2 indica erro de configuração, dependência, lock, persistência ou operação do coletor. Resultados malsucedidos de banda ficam registrados no histórico; a mensagem distingue falha do teste e erro operacional.

### Histórico

As consultas abrem o banco somente para leitura e podem coexistir com o serviço:

```bash
.venv/bin/python collector.py --config config.local.json --history 1h
.venv/bin/python collector.py --config config.local.json --history 24h
.venv/bin/python collector.py --config config.local.json --history 7d
```

A saída é JSON, com métricas por alvo/resolver, incidentes, lacunas, speedtests e estado mais recente. Ela inclui buckets para gráficos e pode ser extensa. Para salvar:

```bash
.venv/bin/python collector.py --config config.local.json --history 24h > /tmp/netmonitor-history.json
```

## Como interpretar os dados

| Observação | Diagnóstico |
| --- | --- |
| Gateway e todos os públicos respondem | Conectividade ICMP observada normal |
| Gateway responde; parte dos públicos falha | Degradação de alvo ou caminho específico |
| Gateway responde; todos os públicos falham | Indício de falha WAN ou filtragem ICMP |
| Gateway e todos os públicos falham | Indício de problema local/link/gateway; WAN indeterminada |
| Gateway falha; algum público responde | Há conectividade externa apesar da ausência de ICMP no gateway |
| DNS falha | Falha de resolução, discriminada por resolver |
| Erro operacional ou dados desatualizados | Estado desconhecido |

Ausência de ICMP não comprova a causa da falha. O link físico da interface é registrado separadamente. O caminho DNS `system` usa os nameservers de `/etc/resolv.conf`, sem reproduzir integralmente NSS ou `/etc/hosts`; uma resposta de cache local não comprova acesso ao resolver recursivo.

RTT usa somente respostas válidas. Perda é `probes sem resposta / probes enviados`, excluindo erros operacionais e rodadas não executadas. Disponibilidade é a proporção de respostas válidas entre os probes enviados e deve ser lida junto à cobertura. Ausência de dados não significa disponibilidade perfeita.

Jitter é a média de diferenças absolutas entre RTTs consecutivos válidos do mesmo alvo. Falhas, lacunas e reinícios interrompem os pares. É variação de RTT, não jitter unidirecional padronizado.

Após atingir o limiar de falhas, o incidente começa na primeira falha observada. Recuperação também exige uma sequência de sucessos. Incidentes abertos não recebem fim inventado; reinícios preservam histórico e registram lacunas sem assumir queda durante o período sem coleta. Não somar incidentes de diferentes escopos como uma única medida de disponibilidade.

Speedtests podem saturar a conexão. Amostras ICMP/DNS durante o teste e cooldown são marcadas; as métricas por alvo incluem o grupo `without_speedtest` para analisar períodos sem essa carga. Os dados e incidentes brutos permanecem preservados.

Velocidades usam Mb/s decimal e são comparadas ao plano registrado em cada teste. Servidor, overhead Ethernet, tráfego simultâneo e hardware influenciam os resultados. Testes consomem dados: 10 segundos a 1 Gb/s em uma direção equivalem a aproximadamente 1,25 GB.

Reinícios planejados do roteador também aparecem nas amostras e podem gerar incidentes. A classificação automática de manutenção programada ainda não está implementada; os eventos permanecem no histórico para interpretação conforme a rotina da rede monitorada.

## Serviço systemd

Em Linux com systemd, gere uma unidade com usuário, pasta e configuração locais. O exemplo usa `~/netmonitor`; entre na pasta real do projeto e use um caminho sem espaços:

```bash
cd ~/netmonitor
.venv/bin/python - <<'PY'
import getpass
from pathlib import Path

root = Path.cwd().resolve()
unit = (root / 'systemd/netmonitor.service').read_text()
unit = unit.replace('User=spohess', 'User=' + getpass.getuser())
unit = unit.replace('/home/spohess/netmonitor', str(root))
unit = unit.replace('/config.json', '/config.local.json')
(root / 'systemd/netmonitor-notebook.service').write_text(unit)
PY
systemd-analyze verify systemd/netmonitor-notebook.service
sudo install -m 644 systemd/netmonitor-notebook.service /etc/systemd/system/netmonitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now netmonitor.service
```

Encerre a coleta manual antes de ativar o serviço. A unidade executa como usuário normal, inicia após reboot, reinicia em caso de falha e envia logs ao journal. Não espera internet nem inicia GUI. O arquivo de unidade gerado é ignorado pelo Git.

```bash
systemctl status netmonitor.service
journalctl -u netmonitor.service -n 100 --no-pager
journalctl -u netmonitor.service -f
sudo systemctl stop netmonitor.service
sudo systemctl restart netmonitor.service
```

Se atingir o limite de reinícios, corrija o erro mostrado no journal e execute `sudo systemctl reset-failed netmonitor.service` antes de iniciar novamente.

### Serviço existente usando um perfil versionado

Se o serviço ainda aponta para `config.notebook.json`, crie a configuração local e confira os valores específicos da máquina. `cp -n` preserva uma cópia local já existente:

```bash
cp -n config.notebook.json config.local.json
nano config.local.json
sudo sed -i 's@/config.notebook.json@/config.local.json@g' /etc/systemd/system/netmonitor.service
sudo systemctl daemon-reload
sudo systemctl restart netmonitor.service
systemctl cat netmonitor.service
```

O comando de substituição é para Linux e para a unidade instalada pelo procedimento acima. Confirme que `ExecStart` usa `config.local.json`. A partir daí, ajuste interface, gateway, plano e IP esperado somente nesse arquivo; mantenha os perfis versionados como exemplos. O `.gitignore` já exclui `config.local.json`; arquivos previamente rastreados pelo Git continuam rastreados mesmo se forem adicionados ao ignore.

## Dados, backups e atualizações

O SQLite usa WAL, `busy_timeout` limitado e `synchronous=NORMAL`. Há um único escritor. Retenção remove dados em lotes e preserva incidentes abertos. Corte de energia pode perder commits recentes; não há promessa de perda zero. Disco cheio, fila cheia ou erro de escrita são reportados, com saída controlada.

Para inspecionar o banco padrão:

```bash
sqlite3 data/monitor.db 'PRAGMA quick_check;'
sqlite3 data/monitor.db 'SELECT status, count(*) FROM probe_samples GROUP BY status;'
du -h data/monitor.db*
```

Antes de atualizar código que migra o schema, pare o serviço e faça backup consistente:

```bash
sudo systemctl stop netmonitor.service
.venv/bin/python - <<'PY'
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

source = Path('data/monitor.db')
backup = Path('data/monitor-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.backup.db')
with sqlite3.connect(source) as original, sqlite3.connect(backup) as destination:
    original.backup(destination)
print(backup)
PY
```

Ajuste o caminho se o banco estiver em outro local. Não copie somente `.db` de um banco ativo em WAL. Preserve `data/` e `config.local.json` nas atualizações. Depois de atualizar, instale dependências se necessário, execute testes e `--once`, e reinicie o serviço. Migrações versionadas atualizam o schema sem apagar o banco.

## Desenvolvimento e testes

```bash
python3 -m unittest discover -s tests -v
```

Os testes usam fixtures, relógios/probes injetados e bancos temporários. Não acessam a internet, não exigem root nem alteram rede ou serviço real. Incluem parser, erros operacionais, timeout/cancelamento, agendamento, incidentes, métricas, migrações, retenção, lock, speedtest e encerramento com flush.

A suíte atual tem **62 testes offline**, executados com sucesso no macOS. Também foram relatadas execuções do coletor em Raspberry Pi OS e Linux Mint, e uma medição manual com o CLI da Ookla. Esses resultados não equivalem a validação de todas as distribuições ou do desempenho sustentado; a integração automática de banda ainda precisa ser confirmada em uso real.

## Estrutura

```text
netmonitor/
├── collector.py
├── database.py
├── speedtest.py
├── config.json
├── config.notebook.json
├── requirements.txt
├── data/
├── tests/
├── systemd/
└── docs/OPERATIONS.md
```

`config.local.json`, bancos/backups, locks, caches, ambientes virtuais, logs e binários locais são excluídos pelo `.gitignore`. Configurações padrão e testes são versionáveis. Não inclua dados de produção no repositório.

Detalhes dos contratos, campos, retenção, cobertura e implantação anterior estão em [docs/OPERATIONS.md](docs/OPERATIONS.md). O [MEMORY.md](MEMORY.md) mantém o contexto operacional do projeto.

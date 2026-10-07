# Netmonitor

Monitor contínuo de estabilidade ICMP e resolução DNS, com teste de banda opcional para o notebook Linux Mint 22.3. O perfil Raspberry Pi 3B mantém speedtest desativado. A Ethernet de 100 Mb/s do Pi não representa a capacidade do plano; o notebook gigabit permite medir download/upload do plano de 1000 Mb/s simétrico.

## Instalação e execução

Requer Python 3.9 ou superior, `fping` e `dnspython`. No Raspberry Pi OS Debian Trixie:

```bash
sudo apt update
sudo apt install python3 python3-venv sqlite3 fping
cd /home/spohess/netmonitor
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
source .venv/bin/activate
fping -C 5 -q 10.10.10.254 1.1.1.1 8.8.8.8
python3 collector.py --help
python3 collector.py --config config.json --once
python3 collector.py --config config.json
```

`sqlite3` da biblioteca padrão faz a persistência; o pacote de mesmo nome acima fornece também a CLI de inspeção. `dnspython` fornece sockets DNS assíncronos com lifetime e cancelamento, evitando chamadas bloqueadas de `getaddrinfo` em threads. As versões declaradas são 2.7.0 para Python 3.9 e 2.8.0 para Python 3.10+. Não instalar com `sudo pip` nem transportar o ambiente virtual do Mac para Linux ARM.

No Mac, instalar `fping` com `brew install fping` se Homebrew estiver disponível. Repetir a criação do ambiente virtual. O caminho `/sys/class/net/eth0` é opcional; a sua ausência não impede execução. Os IPs privados só são acessíveis na rede correspondente.

`--once` executa uma rodada dos alvos ICMP e consultas DNS imediatas, paralelas entre resolvers. Termina mesmo sem resposta, sujeito aos timeouts configurados. Código 0 significa execução concluída, inclusive com perda ICMP ou falha de resolução DNS. Código 2 indica configuração, dependência, lock, erro operacional ou persistência. Se a checagem inicial de `fping --help` falhar, registra uma rodada operacional, encerra com 2 e não consulta DNS. `Ctrl+C` e SIGTERM interrompem novas tarefas, cancelam consultas em voo, terminam/aguardam subprocessos e drenam a fila de eventos já completos. Uma operação cancelada em voo não é contabilizada como probe enviado sem resposta.

Não executar como root. Testar `fping` como `spohess`. Se houver erro de permissão no Pi, verificar o empacotamento antes de mudar privilégios:

```bash
command -v fping
ls -l /usr/bin/fping
getcap /usr/bin/fping
sudo apt install --reinstall fping
```

Se o pacote não fornecer o acesso necessário a ICMP e isso tiver sido confirmado no Pi, a correção restrita ao binário é `sudo setcap cap_net_raw+ep /usr/bin/fping` (requer `libcap2-bin`). Revisar essa necessidade no sistema real; não conceder capacidades ao Python. Atualizações do pacote podem substituir atributos do binário.

## Configuração e caminhos

`--config` relativo é resolvido contra a pasta que contém `collector.py`. `database_path` e caminhos de `fping_binary` contendo `/` são relativos à pasta do arquivo de configuração; caminhos absolutos são preservados. Um nome simples como `fping` usa o PATH. Nenhum caminho depende do diretório corrente do terminal.

Os defaults em `config.json` são:

| Campo | Padrão / semântica |
| --- | --- |
| `targets` | Gateway 10.10.10.254, Cloudflare 1.1.1.1 e Google 8.8.8.8; IDs estáveis |
| `icmp_interval_seconds` | 2 s, uma única rodada em voo |
| `icmp_timeout_ms` | 700 ms por alvo; um único envio, sem retries |
| `process_timeout_seconds` | 1,8 s, também aplicado ao `--help` |
| `dns.interval_seconds` | 30 s, cadência própria |
| `dns.timeout_seconds` | 3 s por resolver, paralelos |
| `dns.name` / `record_type` | example.com / A |
| `dns.resolvers` | system e 1.1.1.1 |
| `failure_threshold` / `recovery_threshold` | 3 falhas / 2 sucessos consecutivos por escopo |
| `database_path` | data/monitor.db, criado automaticamente |
| `retention_days` | 30 dias de amostras brutas e lacunas |
| `incident_retention_days` | 365 dias após encerramento; abertos preservados |
| `maintenance_interval_seconds` | 3600 s; lotes pendentes retomados em no mínimo 1 s |
| `retention_batch_size` | Até 2000 linhas por tabela por execução da manutenção |
| `flush_interval_seconds` | 1 s de espera máxima do escritor; eventos disponíveis são gravados imediatamente |
| `max_queue_size` | 128 eventos; cada rodada ICMP é um evento com todos os alvos |
| `log_level` | INFO; sucessos repetidos não geram logs |

Valida JSON, campos, tipos, limites, IDs/IPs duplicados, gateway único, pelo menos dois WAN e caminho DNS system + explícito. O timeout global deve exceder timeout por alvo + 10 ms por alvo + 100 ms de margem e caber no intervalo ICMP. Timeout DNS deve ser menor que o intervalo DNS. A lista tem limites de 32 alvos e 8 resolvers, e a concorrência DNS é limitada a essa lista. Retenção menor que 7 dias emite aviso explícito. Mudança de endereço ou papel de um alvo existente exige novo ID, preservando a semântica do histórico antigo.

O coletor verifica as opções locais `-C`, `-q`, `-t`, `-p` e `-r` antes de construir os argumentos. Usa `-C 1 -q -r 0`, IPs numéricos e locale C. Argumentos são passados como lista, sem shell. Consulta as três saídas no mesmo processo; examina stdout e stderr. Retornos 0/1 permitem resultados por alvo; 2/3/4, timeout global, binário ausente, permissão, saída inesperada ou duplicada são operacionais. Uma linha faltante ou RTT inválido deixa somente aquele alvo desconhecido. Saída inesperada fora das linhas reconhecidas invalida a rodada, conservadoramente. Saída é limitada a 64 KiB por stream.

## Estados e DNS

| Diagnóstico persistido | Observação |
| --- | --- |
| `icmp_normal` | Gateway e todos os públicos respondem |
| `target_or_path_degraded` | Gateway responde, parte dos públicos falha |
| `wan_unavailable_or_icmp_filtered` | Gateway responde, todos os públicos falham |
| `local_link_or_gateway_wan_unknown` | Gateway e todos os públicos falham; WAN indeterminada |
| `gateway_no_icmp_external_reachable` | Gateway falha, algum público responde |
| `unknown` | Algum alvo teve erro operacional |

Cada alvo mantém escopo `target:<id>`. `wan` avalia alcance externo: qualquer público respondendo significa sucesso; todos falhando só significa falha quando o gateway responde. `local` falha quando gateway e todos os públicos falham, e tem sucesso quando qualquer um responde. Escopos desconhecidos interrompem sequências de confirmação/recuperação. Não somar incidentes de alvo, WAN e local para calcular disponibilidade: eles podem representar a mesma observação.

O estado físico de `eth0` (`operstate` e `carrier`) é guardado separado em cada rodada. Um `carrier` ausente fica nulo, nunca vira perda. Falta de resposta ICMP é um indício, não comprova a causa: roteadores e caminhos podem filtrar ou reduzir prioridade de ICMP.

DNS não altera resultados ICMP. Cada resolver usa escopo `dns:<id>`, com resultados `success`, `nodata`, `nxdomain`, `servfail`, `refused`, `timeout`, `dns_error` ou `operational_error`. `success` e `nodata` indicam consulta concluída sem erro de resolução; NXDOMAIN para o nome configurado é uma falha da consulta, mesmo sendo uma resposta DNS válida. Todas as amostras preservam nome, tipo, caminho/servidor, duração, erro e respostas.

`system` usa os nameservers de `/etc/resolv.conf` via dnspython; o servidor efetivamente usado é registrado quando existe resposta. Não é uma reprodução completa de NSS, `/etc/hosts`, mDNS ou resolvers por domínio do macOS. No Pi, conferir `cat /etc/resolv.conf` e, se disponível, `resolvectl status`. No Mac, conferir também `scutil --dns`. Resolver local em cache não comprova acesso ao recursivo; dnspython não mantém cache próprio entre rodadas, mas caches externos continuam valendo. Comparar system e público explícito permite investigar problema local ou mais amplo de resolução. DNS com timeout ou SERVFAIL indica falha da consulta, sem declarar indisponibilidade WAN.

## Tempo e incidentes

Cadência e duração observada usam monotônico; a persistência usa Unix UTC em microssegundos (campos `*_us`). A próxima rodada segue o prazo original; a execução não é somada ao intervalo. Prazos perdidos são pulados e registrados em `gaps`, sem rajadas. Atrasos superiores a 50 ms também são registrados. Consultas DNS lentas não bloqueiam ICMP.

A primeira falha é preservada nas amostras. Ao atingir 3 falhas consecutivas, o incidente registra início na primeira falha e confirmação na terceira. O primeiro sucesso registra candidato de recuperação; o segundo encerra. Falha, ausência operacional, reinício ou lacuna durante a recuperação invalidam o candidato. Incidente aberto mantém `ended_us` nulo.

Intervalos monotônicos entre observações consecutivas de falha, incluindo o intervalo até o primeiro sucesso, somam `observed_seconds`. Intervalos durante confirmação de recuperação não são somados. Lacunas maiores que 1,75 vezes a cadência e reinícios interrompem continuidade; não se atribui duração de queda ao tempo sem coleta. Incidentes permanecem abertos e recebem `quality=gapped` e contagem de lacunas. O histórico de execuções preserva início, encerramento e motivo; um processo abruptamente morto pode ter encerramento nulo. A próxima execução registra `restart` com referência à última amostra e ao encerramento anterior, sem inventar observações.

Segmentos de duração observada são compactados em grupos contínuos de até 60 s. A duração total usa monotônico. O recorte de um segmento em uma janela UTC usa proporção linear entre seus extremos, portanto é uma estimativa ao longo desse minuto. Se o relógio civil voltar, o segmento mantém duração monotônica positiva e marca qualidade com lacuna, mas não é projetado sobre janelas civis. `civil_span_seconds` é o intervalo civil recortado, limitado a não negativo, e não representa duração observada de queda. Para abertos, o extremo da janela não é um fim persistido. Mudanças de NTP não alteram o agendamento nem produzem duração observada negativa.

## Histórico e métricas

Consultas não adquirem o lock exclusivo do coletor e abrem conexão própria somente leitura:

```bash
python3 collector.py --config config.json --history 1h
python3 collector.py --config config.json --history 24h
python3 collector.py --config config.json --history 7d
```

A API `History.window()` ou `History.query(start_us, end_us, ...)` permite outras consultas. Limites são início inclusivo e fim exclusivo. Retorna métricas por alvo e resolver, incidentes sobrepostos mesmo quando começaram antes da janela, lacunas e buckets (no máximo 120 por série por padrão, configurável até 1000). Buckets são alinhados a múltiplos do intervalo correspondente, com contagens, médias, máximos, perda e cobertura. Não retorna todas as amostras brutas ao navegador. Lacunas e incidentes são paginados em até 1000 registros; `gaps_total`/`incidents_total` e `gaps_next_id`/`incidents_next_id` sinalizam mais resultados. Usar `History.gaps()` ou `History.incidents()` com `after_id` para as próximas páginas, mantendo os mesmos limites da janela.

- RTT é finito e não negativo, em ms; falhas têm nulo. Média/máximo usam apenas respostas válidas.
- `sent = success + loss`; erros operacionais e rodadas não executadas ficam fora do denominador. `loss = lost / sent` e `availability = received / sent`. Sem envios observados, ambas são nulas.
- `jitter_ms` é a média de `abs(RTT atual − RTT anterior)` entre pares consecutivos válidos do mesmo alvo na janela. Falha, erro, troca de execução ou lacuna maior que 1,75 vezes a cadência interrompem pares. Um par basta; sem pares, é nulo. É variação de RTT, não jitter unidirecional padronizado.
- `expected_slots` estima oportunidades pela duração civil da janela dividida pelo intervalo informado, arredondando para cima, nunca abaixo das amostras efetivas. `coverage = sent / expected_slots`; `execution_coverage = samples / expected_slots`. Inclui todo o período da janela, inclusive antes da primeira execução e lacunas. É cobertura de amostragem estimada, não disponibilidade temporal. Alterações históricas de intervalo ou relógio exigem interpretar a estimativa com os `collector_runs`, que guardam o intervalo usado.
- DNS oferece contagens por resultado, duração média/máxima nos buckets, cobertura e `query_availability`: success + nodata sobre resultados não operacionais, inclusive timeouts.
- `history_started_us`, `retained_since_us` e `data_removed` informam início do histórico e remoção por retenção. Uma janela sem dados não indica conectividade perfeita.
- `latest_icmp` e `latest_dns` incluem idade, `stale` e estado atual desconhecido após 3 intervalos sem atualização. Horários futuros causados por ajuste civil não permitem concluir frescor por si só; conferir execuções e lacunas.

## SQLite, retenção e falhas

Schema versionado com migrações transacionais (v1: coleta/estados; v2: segmentos de incidentes; v3: speedtests e vínculos das amostras ICMP/DNS). Migração não apaga o banco. IDs únicos de rodada/amostra, índice único parcial de incidente aberto por escopo, índices de alvo/tempo, resolver/tempo, rodada/tempo e incidente/segmento. SQL usa parâmetros; nomes de tabelas da manutenção vêm de uma lista fixa interna.

Um único escritor no event loop grava transações por rodada ou pequeno lote, sem compartilhar conexões com threads. Conexões de histórico são separadas. WAL + `busy_timeout=2000` permitem leitores simultâneos com espera limitada. `synchronous=NORMAL` reduz sincronizações e desgaste do SD mantendo atomicidade transacional; corte de energia pode perder commits recentes. Não se promete perda zero. O lock `flock` é mantido pelo descritor aberto durante toda a execução e adquirido antes de abrir/migrar o banco. O arquivo pode continuar existindo após término, sem significar lock ativo; não removê-lo durante coleta.

Disco cheio, permissão, corrupção ou fila cheia produzem log e saída controlada. Não há retry interno indefinido. O systemd limita tentativas via StartLimit. Eventos completos já enfileirados são gravados no encerramento normal; erro de escrita pode impedir essa drenagem e é reportado. Chamadas SQLite síncronas têm espera de lock limitada; I/O ruim no SD pode atrasar o event loop, sendo registrado como atraso nas próximas rodadas. Não há garantia de tempo real.

Limpeza usa lotes periódicos, sem VACUUM a cada rodada; incidentes abertos são preservados. Arquivo pode não diminuir imediatamente após exclusões, pois páginas são reutilizadas. Segmentar incidentes reduz crescimento nas quedas prolongadas. Três alvos a cada 2 s produzem aproximadamente 129.600 amostras/dia, 3,89 milhões em 30 dias, além de rodadas, DNS, índices e WAL. Medir tamanho e espaço no Pi; não foi estimado desempenho real do cartão.

```bash
df -h /home/spohess/netmonitor
du -h data/monitor.db*
sqlite3 data/monitor.db 'PRAGMA quick_check;'
sqlite3 data/monitor.db 'SELECT status, count(*) FROM probe_samples GROUP BY status;'
sqlite3 data/monitor.db 'SELECT scope, started_us, confirmed_us, recovery_us, ended_us, observed_seconds, quality FROM incidents ORDER BY id DESC LIMIT 20;'
```

## Transferência limpa por scp

Para a primeira instalação, preparar uma pasta limpa, sem banco, WAL/SHM, locks, caches ou `.venv`, a partir do diretório do projeto:

```bash
staging_dir=$(mktemp -d)
mkdir -p "$staging_dir/netmonitor/data"
cp collector.py database.py speedtest.py config.json config.notebook.json requirements.txt AGENTS.md MEMORY.md README.md .gitignore "$staging_dir/netmonitor/"
cp -R tests systemd "$staging_dir/netmonitor/"
find "$staging_dir/netmonitor" -type d -name __pycache__ -exec rm -rf {} +
cd "$staging_dir"
scp -r netmonitor spohess@rbp-spohess.local:/home/spohess/
```

Se mDNS falhar, substituir o último comando por:

```bash
scp -r netmonitor spohess@10.10.10.5:/home/spohess/
```

Não distribuir dados reais. Criar ambiente virtual no Pi. Nas atualizações, preservar `data/` e a configuração personalizada; garantir os diretórios de destino existentes. Antes de migrar, parar o serviço e fazer backup consistente no Pi:

```bash
sudo systemctl stop netmonitor.service
cd /home/spohess/netmonitor
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

Se o banco estiver em outro caminho configurado, ajustar `source`. Não copiar somente `.db` de um escritor em WAL. Após o backup, no Mac:

```bash
scp collector.py database.py speedtest.py requirements.txt AGENTS.md MEMORY.md README.md spohess@10.10.10.5:/home/spohess/netmonitor/
scp tests/*.py spohess@10.10.10.5:/home/spohess/netmonitor/tests/
scp systemd/netmonitor.service spohess@10.10.10.5:/home/spohess/netmonitor/systemd/
```

Enviar `config.json` somente após revisar diferenças. No Pi, reinstalar dependências no venv se mudaram, rodar testes e `--once`, então reiniciar o serviço. A migração acontece ao abrir o banco como coletor. Manter backup para recuperação; nunca reverter apagando dados silenciosamente.

## Banda no notebook Linux Mint 22.3

`config.notebook.json` habilita o teste de banda: plano de **1000 Mb/s de download e upload**, IP estático, primeira execução após 5 minutos e repetição a cada 6 horas. Configurações antigas sem `speedtest` e o `config.json` do Pi continuam com o teste desativado.

A dependência é o [CLI oficial Speedtest by Ookla](https://speedtest-static-dev.speedtest.dev/apps/cli), executado em subprocesso assíncrono, sem shell, com stdin fechado e timeout de 180 s. O pacote Python `speedtest-cli` tem argumentos/formato diferentes e não serve para este adaptador. Não há dependência Python extra.

No notebook, depois de transferir uma pasta limpa e criar o venv, conferir arquitetura e interface Ethernet:

```bash
cd ~/netmonitor
uname -m
ip route get 1.1.1.1
```

Para `x86_64`, instalar o arquivo Linux publicado pela Ookla abaixo; para outra arquitetura, selecionar o download correspondente na página oficial:

```bash
sudo apt install curl ca-certificates
download_dir=$(mktemp -d)
curl -fL https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-x86_64.tgz -o "$download_dir/speedtest.tgz"
tar -xzf "$download_dir/speedtest.tgz" -C "$download_dir"
mkdir -p bin
install -m 755 "$download_dir/speedtest" bin/speedtest
bin/speedtest --help
bin/speedtest
```

Na execução manual, ler e aceitar os termos apresentados se concordar. O coletor não passa `--accept-license` ou `--accept-gdpr`, não aceita termos automaticamente e deve usar o mesmo usuário da aceitação. `bin/speedtest` também faz um teste real. Falta de aceitação ou falha de servidor/conectividade pode produzir `failed`; binário ausente/incompatível, JSON inválido ou timeout global produz `operational_error`. Velocidades ficam nulas, nunca zero, e não alteram resultados ICMP.

Editar `interface` em `config.notebook.json` para a Ethernet mostrada por `ip route`, como `enp2s0`; `eth0` é o valor herdado do Pi. O CLI é ligado explicitamente a essa interface. O binário `bin/speedtest` é relativo à configuração; caminho absoluto também é permitido. Não alterar a rede para ajustar o monitor.

```bash
source .venv/bin/activate
python3 -m unittest discover -s tests -v
python3 collector.py --config config.notebook.json --once
python3 collector.py --config config.notebook.json --once --speedtest
python3 collector.py --config config.notebook.json
```

`--once` continua somente ICMP/DNS mesmo com banda habilitada. `--once --speedtest` inclui um teste imediato, limitado pelo timeout e cooldown. Parar o serviço antes de executar outra instância manual contra o mesmo banco. No modo contínuo, ICMP/DNS mantêm seus agendamentos durante o teste e speedtests não se sobrepõem. Parada cancela/aguarda o processo e registra `cancelled`. Morte abrupta deixa `running`, convertido no próximo início em `interrupted`, sem fim ou duração inventados.

Cada teste registra início, fim, duração monotônica, download/upload em Mb/s decimal, bytes transferidos, latência do CLI, servidor, interface, IP público e plano contratado. `bandwidth` é convertido de bytes/s por × 8 ÷ 1.000.000. Histórico de 1h/24h/7d inclui `speedtests`: percentuais do plano, médias/máximos, bytes e resultados paginados; `History.speedtests(..., after_rowid=cursor)` continua a página. Retenção acompanha as amostras brutas, preservando testes `running` e resultados referenciados por amostras retidas.

`ip_mode=static` registra a característica informada do plano. `expected_public_ip` é opcional e inicialmente nulo: preenchê-lo com o IP contratado habilita `public_ip_matches_expected`. Não se configura IP na interface nem se presume seu valor. Famílias IP diferentes ou múltiplos IPs de saída podem afetar a comparação; divergência não altera o diagnóstico ICMP.

Speedtests podem saturar o link e consumir muitos dados: 10 segundos a 1 Gb/s em uma direção equivalem a cerca de 1,25 GB. O total real depende do CLI. Intervalo mínimo configurável: 15 minutos; padrão: 6 horas. O resultado depende do servidor, overhead Ethernet, uso simultâneo e hardware; não comprova capacidade máxima do roteador ou entrega garantida do plano.

Amostras ICMP/DNS sobrepostas ao teste e aos 10 s de cooldown recebem `speedtest_id`. Buckets indicam `speedtest_samples`; cada alvo tem métricas totais e `without_speedtest`, cujo jitter não atravessa amostras carregadas. A cobertura desse grupo desconta apenas slots observados marcados e continua estimada. Perdas e incidentes brutos são preservados, inclusive sob carga. O reboot diário do roteador às 03h permanece manutenção planejada para o dashboard; sua duração não é presumida por esta extensão.

Para preparar a unidade no notebook, gerar uma cópia com usuário e pasta locais, preservando o arquivo do Raspberry. Usar `~/netmonitor`, sem espaços:

```bash
python3 - <<'PY'
import getpass
from pathlib import Path

root = Path.cwd().resolve()
unit = (root / 'systemd/netmonitor.service').read_text()
unit = unit.replace('User=spohess', 'User=' + getpass.getuser())
unit = unit.replace('/home/spohess/netmonitor', str(root))
unit = unit.replace('/config.json', '/config.notebook.json')
(root / 'systemd/netmonitor-notebook.service').write_text(unit)
PY
systemd-analyze verify systemd/netmonitor-notebook.service
sudo install -m 644 systemd/netmonitor-notebook.service /etc/systemd/system/netmonitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now netmonitor.service
journalctl -u netmonitor.service -n 50 --no-pager
```

Encerrar coleta manual antes de ativar. Não houve instalação ou acesso ao notebook pelo agente. Não copiar ambientes virtuais do Mac ou Pi. Migrar histórico exige backup SQLite consistente com escritor parado e IDs/endereço/papel preservados; dados de duas máquinas medindo caminhos diferentes não devem ser fundidos como uma série sem identificação.

## Serviço systemd

`systemd/netmonitor.service` usa `User=spohess`, caminhos absolutos de projeto/venv/configuração, `Restart=on-failure`, espera de 10 s, no máximo 5 tentativas em 120 s e parada de 20 s. Depende somente de `network.target`; não espera internet nem inicia GUI. Não aplica sandbox/capabilities não validadas no Pi.

Encerrar a execução manual com Ctrl+C antes de ativar. No Pi, após validar como usuário do serviço:

```bash
systemd-analyze verify systemd/netmonitor.service
sudo install -m 644 systemd/netmonitor.service /etc/systemd/system/netmonitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now netmonitor.service
systemctl status netmonitor.service
journalctl -u netmonitor.service -n 100 --no-pager
journalctl -u netmonitor.service -f
sudo systemctl stop netmonitor.service
sudo systemctl restart netmonitor.service
```

Depois de um reboot autorizado, verificar `systemctl is-enabled netmonitor.service`, `systemctl status netmonitor.service` e horários recentes no banco. Se o limite de reinícios for atingido, corrigir o motivo e executar `sudo systemctl reset-failed netmonitor.service` antes de iniciar. Não matar processos Python indiscriminadamente.

## Testes e validação pendente

```bash
python3 -m unittest discover -s tests -v
```

Os testes usam apenas biblioteca padrão, fixtures e bancos temporários. Não fazem acesso externo, não exigem root, não alteram rede nem iniciam serviço. O adaptador dnspython é testado com resultados/exceções injetados, e o coletor recebe probes, DNS e relógios substituíveis. Os testes de subprocesso executam somente filhos locais; um teste envia SIGTERM a um coletor filho com probes falsos e verifica flush/encerramento.

Cobrem parser, perdas/saída parcial/stderr, erros operacionais, timeout/cancelamento/output limitado, cadência/atraso/relógio civil, diagnósticos, incidentes/reinício/lacunas, métricas/janelas/buckets, schema/migração/rollback, retenção, lock entre processos, persistência e flush.

Coletor original testado no Mac com Python 3.9.6. O usuário depois confirmou serviço após reboot, ICMP, DNS real e persistência no Raspberry; ver MEMORY.md. A extensão de banda foi testada com fixtures offline, sem medição real. A instalação de dnspython foi impedida pelo acesso de rede do ambiente de desenvolvimento; DNS real já foi confirmado no Pi; a execução no notebook permanece pendente. `fping --help` local confirmou as opções usadas. Configuração, sintaxe Python e estrutura da unidade systemd foram verificadas localmente; a validação real com `systemd-analyze` requer Linux/systemd. Não houve SSH ou instalação remota pelo agente; a operação no Raspberry foi confirmada por resultados fornecidos pelo usuário. A extensão ainda não foi validada no notebook.

Antes de declarar o MVP validado no hardware: executar `fping` como spohess, `--once`, coletar alguns minutos, inspecionar SQLite, medir CPU/RAM/tamanho/WAL, testar parada limpa, verificar a unidade com `systemd-analyze` e conferir após reboot autorizado. Não simular queda desligando uma rede remota. Preservar eth0, drivers LCD SPI, orientação, calibração XPT2046 e labwc. Dashboard web/quiosque fica para etapa posterior.

Referências: [fping, opções e códigos de saída](https://www.fping.org/fping.8.html), [dnspython, resolução assíncrona](https://dnspython.readthedocs.io/en/stable/async-resolver.html), [dnspython 2.8, Python 3.10+](https://www.dnspython.org/news/2.8.0/).

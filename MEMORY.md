# Estado do projeto — 2026-10-07

O usuário confirmou que o MVP está funcionando no Raspberry Pi. Antes do dashboard, autorizou testes de banda para um notebook Linux Mint 22.3 com Ethernet gigabit, que já fica ligado monitorando sites. O dashboard continua como etapa posterior; não iniciar sem solicitação.

## Evidências recebidas do Raspberry

- Serviço `netmonitor.service` habilitado e ativo após reboot, executando como spohess pelo ambiente virtual em `/home/spohess/netmonitor/.venv`.
- Consulta de histórico retornou dados persistidos, duas lacunas de reinício e nenhum incidente confirmado na janela apresentada.
- Última amostra ICMP: gateway 0,892 ms, Cloudflare 12,3 ms e Google 11,5 ms; diagnóstico normal e dados recentes.
- Interface eth0 com operstate up e carrier true.
- DNS pelo gateway 10.10.10.254 e pelo resolver público 1.1.1.1 respondeu com sucesso.
- Após o reboot houve falhas ICMP e timeouts DNS durante a inicialização, seguidos de recuperação. Também apareceu um timeout operacional isolado de fping, seguido de retorno ao ICMP normal; não contado como perda de pacotes.

Essas evidências foram fornecidas pelo usuário. Não houve acesso SSH pelo agente. O funcionamento real de DNS no Pi foi confirmado pelas amostras, embora a instalação local de dnspython no Mac tenha sido bloqueada pelo acesso de rede do ambiente.

## Validação e limites

- 43 testes offline passaram no Mac com Python 3.9.6.
- A operação e o início automático no Pi estão confirmados pelos resultados acima.
- Não afirmar estabilidade contínua, consumo de CPU/RAM/disco ou desempenho sustentado no Pi: ainda não foram medidos de forma suficiente.
- Sugestão operacional: coletar por 24 horas para uma primeira avaliação de continuidade, cobertura, erros operacionais e crescimento do banco. Sete dias fornecem uma base mais ampla; não são pré-requisito para criar o dashboard. A coleta pode continuar enquanto a interface é desenvolvida.

## Próxima etapa: dashboard

Dashboard web local, processo independente e leitor do SQLite existente. Acesso pelo Mac e layout para o touchscreen 480×320 do Pi: estado atual/frescor, RTT, perda, variação de RTT, incidentes, janelas 1h/24h/7d e gráficos com poucos pontos.

Usar ativos locais, controles grandes por toque, sem hover obrigatório ou rolagem horizontal, com baixo consumo. Definir endereço, porta e acesso local antes de exposição. Não habilitar acesso público ou encaminhamento de portas automaticamente.

Preservar banco de produção, configuração personalizada, serviço coletor, rede, drivers da tela, orientação e calibração. Integração do quiosque deve verificar navegador e autostart da sessão labwc existente, sem presumir X11 ou LXDE.

## Manutenção programada do roteador

O usuário informou que o roteador reinicia diariamente às 03h da manhã, no horário local America/Sao_Paulo. Essa interrupção é esperada e deve ser identificada como manutenção programada, separada de falhas inesperadas no dashboard.

O coletor atual preserva as perdas e pode confirmar incidentes durante esse reboot; ainda não existe classificação automática de manutenção. Manter os dados brutos e explicitar, na apresentação futura, métricas totais e métricas descontando manutenção quando aplicável. A duração usual do reboot e os limites da janela de manutenção ainda não foram informados; não presumir que toda interrupção próxima das 03h seja planejada nem ocultar indisponibilidade prolongada.


## Extensão autorizada: banda no notebook

O usuário decidiu não usar mais o Raspberry para este projeto. O destino atual é o notebook Linux Mint 22.3; referências ao Pi documentam a etapa anterior e não autorizam novas instalações nele.

O plano é de 1000 Mb/s de download e upload, com IP público fixo; o valor do IP não foi informado. Não modificar rede nem presumir o endereço.

Adaptador para CLI oficial Ookla, configuração opcional e schema v3 implementados. config.notebook.json habilita testes a cada 6 horas, após 5 minutos, com timeout de 180 s. Configurações antigas e config.json do Pi mantêm testes desativados. O notebook exige escolher a Ethernet real, instalar binário Linux e aceitar termos manualmente como usuário do serviço. Não houve instalação remota nem teste real de banda.

Histórico registra velocidades, percentuais do plano, bytes, servidor e IP público; comparação com IP contratado é opcional. ICMP/DNS durante teste e cooldown são marcados. Métricas incluem total e grupo sem carga de speedtest. Falha de teste não vira perda ICMP nem velocidade zero. Dashboard ainda não implementado.

Validação da extensão: 62 testes offline passaram no Mac, incluindo conversão de unidades, migração v2→v3, marcação de carga, métricas sem speedtest, cancelamento, retenção e compatibilidade de configuração. Medição real e instalação no Mint permanecem pendentes.

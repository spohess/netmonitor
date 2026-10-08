# Planejamento do dashboard

Data: 08/10/2026. Escopo: apresentação dos dados existentes; implementação e implantação são etapas futuras.

## Objetivo e contexto

Responder rapidamente: a conexão está funcionando agora? Houve degradação no período? O problema observado foi no gateway, nos alvos externos ou no DNS? Qual banda foi medida e quando?

O destino atual é o notebook Linux Mint, conforme MEMORY.md. A proposta é um dashboard web local acessível pelo navegador do notebook e, posteriormente, por outros dispositivos da rede mediante definição de acesso. O antigo touchscreen do Raspberry não é requisito da primeira versão.

O coletor Python mantém o único escritor SQLite. `database.py:History` já oferece leitura somente leitura, janelas 1h/24h/7d, métricas por alvo/resolver, buckets, incidentes, lacunas, frescor e speedtests. Essa camada será a base do dashboard, preservando as regras do MVP.

## Evidência local

Inspeção somente leitura em 08/10/2026: schema v3, 45.067 rodadas, 135.201 amostras ICMP, 6.012 consultas DNS, um incidente fechado no alvo Google, três registros de lacuna e seis speedtests (cinco sucessos e uma falha). As contagens são uma fotografia e podem crescer durante a coleta.

O intervalo ICMP consultado foi de 07/10/2026 13:50 UTC até 08/10/2026 14:52 UTC, aproximadamente 25 horas. Já há dados suficientes para desenvolver e validar a apresentação. Uma janela de sete dias ainda inclui tempo anterior à coleta; cobertura deve deixar isso explícito.

Uma consulta completa de 24 horas via `History.window` levou aproximadamente 0,99 s nesta execução local. É uma medição isolada, não um benchmark. Ela justifica separar estado atual de histórico e medir desempenho antes de definir atualização frequente.

## Organização da interface

### Visão geral

- Cabeçalho com período 1h/24h/7d, horário da atualização e fuso America/Sao_Paulo.
- Faixa de estado: diagnóstico ICMP observado, idade da amostra e estado físico da interface quando disponível. DNS terá indicador próprio.
- Resumo por alvo: RTT médio/máximo, perda, disponibilidade observada e cobertura. Gateway e alvos WAN permanecem separados.
- Banda: último teste bem-sucedido, data, download/upload em Mb/s e percentual do plano registrado naquele teste; mostrar também o resultado da tentativa mais recente.
- Gráfico de RTT com séries por alvo, média e máximo por bucket; gráfico separado de perda e indicação de cobertura.
- Linha de eventos distinguindo incidentes, períodos de speedtest e lacunas de coleta.
- Lista resumida de incidentes recentes, com acesso ao detalhamento.

Layout inicial para desktop: estado no topo, resumos abaixo, gráficos na região central e eventos no rodapé. Em telas menores, blocos empilhados, sem rolagem horizontal. Cores acompanhadas por texto, unidades explícitas e detalhes acessíveis por clique ou toque.

### Detalhamento

- Estabilidade: métricas e gráficos por alvo, contagens e explicação dos denominadores.
- DNS: resultados por resolver, duração e cobertura; não transformar timeout DNS em perda ICMP.
- Banda: medições discretas de download/upload, plano de referência, servidor, duração, bytes e resultado. Sem interpolar velocidade entre testes.
- Incidentes: escopo, início, confirmação, recuperação, estado aberto/fechado, duração observada e qualidade da observação.
- Qualidade dos dados: lacunas, erros operacionais, início do histórico e limites de retenção.

## Regras de interpretação

- Banco ausente, vazio, inacessível ou coleta desatualizada devem ter estados explícitos. Sem observações, métricas ficam indisponíveis, nunca zero ou 100%.
- Disponibilidade é proporção de respostas ICMP válidas entre probes enviados, por alvo. Não será chamada de SLA nem disponibilidade temporal da internet.
- Cobertura acompanha disponibilidade e perda. Diferenciar cobertura da medição de cobertura da execução quando houver erro operacional.
- Variação de RTT usa `jitter_ms` existente, com descrição de sua fórmula. Não pressupor jitter unidirecional.
- Não ligar linhas através de buckets sem respostas; explicitar baixa cobertura mesmo quando houver média válida.
- A janela de histórico não altera o estado atual: este usa as últimas observações e o relógio atual do servidor.
- Frescor ICMP e DNS é independente. Preservar o limiar existente de três intervalos e sinalizar horários futuros incompatíveis com o relógio atual.
- Mostrar métricas totais e resumo ICMP sem carga de speedtest. Os buckets existentes não oferecem séries completas filtradas; não rotular os gráficos totais como filtrados. Essa expansão exige consulta adicional.
- Falha de speedtest não é velocidade zero. O último sucesso não pode esconder uma tentativa recente malsucedida. Banda antiga não representa velocidade atual.
- IP público é observado pelo speedtest, com idade da observação; comparação só existe se houver IP esperado configurado.
- Incidente aberto continua aberto. Duração observada e intervalo civil são valores diferentes; lacunas não são somadas como queda comprovada.
- Não somar incidentes sobrepostos de escopos diferentes para criar uma indisponibilidade global.
- O reboot diário do roteador às 03h é contexto de manutenção prevista. Classificação automática e métricas descontadas dependem da duração e dos limites acordados; proximidade do horário não basta para classificar uma falha.

## Arquitetura proposta

Processo web Python independente → camada de apresentação/consultas → `History` → SQLite existente, somente leitura. O navegador recebe JSON e arquivos estáticos locais. A escolha da biblioteca web e de gráficos fica para a implementação, com preferência por poucas dependências e sem CDN obrigatória.

Contratos propostos:

- `GET /api/status`: últimas observações ICMP/DNS, frescor, link e tentativas de banda recentes, inclusive fora da janela selecionada.
- `GET /api/history?window=24h`: resumos e séries agregadas da janela; reutilizar as fórmulas existentes.
- `GET /api/incidents` e `GET /api/gaps`: paginação com limites fixos de janela.
- `GET /api/speedtests`: resultados paginados e resumo da janela.

Ainda não existe consulta leve específica de status nem busca independente da última tentativa/último sucesso de banda. Será necessário acrescentar essas leituras, sem iniciar o coletor ou migrar o banco pela aplicação web.

Cada requisição usa conexão própria; transações de leitura curtas e snapshot coerente dentro da resposta. Validar schema e parâmetros, limitar períodos/pontos/páginas e tratar erros sem expor caminhos internos.

Atualização inicial proposta: status a cada 5 s; histórico a cada 60 s ou ao trocar o período. Evitar requisições sobrepostas, reduzir atividade com aba oculta e manter timestamp dos dados quando houver falha. Cache breve por janela e compartilhamento entre clientes dependem da medição de carga.

Configuração web em arquivo separado: caminho do banco, endereço, porta e fuso. Não inserir campos web no JSON do coletor, cuja validação é restrita. Desenvolvimento em loopback; acesso pela LAN depende da definição de endereço e controle de acesso. Preparar uma unidade systemd independente em etapa posterior, sem instalá-la automaticamente.

## Sequência de entrega

1. Camada de leitura e API: contratos, status leve, histórico agregado, últimas medições de banda e tratamento de erros. Validar com banco temporário e leitura concorrente ao escritor.
2. Visão geral funcional: estado, período, métricas por alvo, gráficos, banda e eventos. Usar dados locais e fixtures claramente identificadas apenas nos testes.
3. Detalhamentos: DNS, speedtests, incidentes paginados, lacunas e métricas ICMP sem carga. Validar estados vazios, antigos, abertos e malsucedidos.
4. Validação de uso e desempenho: navegador desktop e tela estreita, carga de 7 dias em banco temporário, múltiplos leitores e coleta contínua. Medir tempo das consultas, tamanho das respostas e efeito na cadência do coletor.
5. Preparação operacional: documentação e unidade de serviço independente. Definir acesso local antes de qualquer instalação ou exposição.

Manutenção programada automática, exportação de relatórios, alertas, controle do coletor e disparo de speedtest pela interface ficam para incrementos posteriores. A primeira entrega é de apresentação e consulta.

## Critérios de aceite

- Valores e unidades conferem com `History` para a mesma janela e instante de referência.
- Não há escrita no banco pelo dashboard nem interrupção do coletor.
- Coleta parada produz estado desconhecido; lacunas, erros e ausência de histórico não aparecem como perda.
- Incidentes abertos não recebem término fictício e paginação não perde registros.
- Speedtest malsucedido, banda antiga e amostras sob carga aparecem corretamente.
- Gráficos usam pontos agregados, preservam ausência de dados e funcionam sem serviços externos de assets.
- Layout funciona por teclado e toque, com informação além das cores.
- Testes novos cobrem contratos e regras de apresentação; executar também `python3 -m unittest discover -s tests -v` ao alterar o MVP.

## Definições pendentes

O planejamento assume navegador no notebook como uso principal. Confirmar na implementação se haverá painel sempre aberto em tela dedicada, acesso por celular/outro computador e preferência visual. Para exposição na LAN, definir endereço, porta e necessidade de autenticação. Para manutenção automática, informar duração usual e limites da janela do reboot das 03h.

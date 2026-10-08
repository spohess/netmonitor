'use strict';

const $ = id => document.getElementById(id);
const escapeText = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const number = (value, digits = 1) => value === null || value === undefined || !Number.isFinite(value) ? '—' : value.toLocaleString('pt-BR', {minimumFractionDigits: digits, maximumFractionDigits: digits});
const percent = value => value === null || value === undefined ? '—' : value > 0 && value < .0001 ? '<0,01%' : value < 1 && value > .9999 ? '>99,99%' : `${number(value * 100, 2)}%`;
const speedPercent = value => value === null || value === undefined ? '—' : `${number(value, 1)}% do plano`;
const labels = {success:'Sucesso',loss:'Sem resposta',operational_error:'Erro operacional',failed:'Falhou',running:'Em execução',cancelled:'Cancelado',interrupted:'Interrompido',unknown:'Desconhecido',timeout:'Timeout',nodata:'Sem registros',nxdomain:'Nome inexistente',no_nameservers:'Falha do resolver'};
const state = {window:'24h', history:null, status:null, statusReceived:0, timezone:'America/Sao_Paulo', errors:{}, historySequence:0, historyController:null, statusBusy:false, paging:{}, speedSignature:null, dnsSignature:null};

function date(value, short = false) {
  if (value === null || value === undefined) return '—';
  return new Intl.DateTimeFormat('pt-BR', {timeZone:state.timezone, ...(short ? {} : {day:'2-digit',month:'2-digit'}), hour:'2-digit',minute:'2-digit', ...(short ? {} : {second:'2-digit'})}).format(new Date(value / 1000));
}
function duration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${number(seconds, seconds < 10 ? 1 : 0)} s`;
  if (seconds < 3600) return `${number(seconds / 60, 1)} min`;
  if (seconds < 86400) return `${number(seconds / 3600, 1)} h`;
  return `${number(seconds / 86400, 1)} dias`;
}
function tag(text, kind = 'neutral') { return `<span class="tag ${kind}">${escapeText(text)}</span>`; }
function outcome(status) { return tag(labels[status] || status || 'Sem dados', ['success','nodata'].includes(status) ? 'good' : ['failed','loss','timeout','nxdomain','no_nameservers'].includes(status) ? 'bad' : status === 'running' ? 'warning' : 'neutral'); }
function empty(text, detail = '') { return `<div class="empty-state">${escapeText(text)}${detail ? `<p>${escapeText(detail)}</p>` : ''}</div>`; }
function widths(root = document) {
  root.querySelectorAll('[data-width]').forEach(el => {el.style.width = `${Math.max(0, Math.min(100, Number(el.dataset.width) || 0))}%`;});
  root.querySelectorAll('[data-height]').forEach(el => {el.style.height = `${Math.max(0, Math.min(100, Number(el.dataset.height) || 0))}%`;});
}
function reportError(kind, message) {
  if (message) state.errors[kind] = message;
  else delete state.errors[kind];
  $('error').hidden = !Object.keys(state.errors).length;
  $('error').textContent = Object.values(state.errors).join(' ');
}
async function request(path, signal) {
  const response = await fetch(path, {signal, cache:'no-store'});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || 'Não foi possível consultar os dados.');
  return result;
}
function current(item, interval) {
  if (!item) return null;
  const elapsed = (performance.now() - state.statusReceived) / 1000;
  const delta = (state.status.generated_us - item.timestamp_us) / 1e6 + elapsed;
  return {...item, age_seconds:Math.max(0, delta), stale:item.clock_anomaly || delta > interval * 3};
}
function renderStatus(updatePanels = true) {
  if (!state.status) return;
  const data = state.status;
  const latest = current(data.latest_icmp, data.latest_icmp?.interval_seconds || 2);
  const diagnosis = latest && !latest.stale ? latest.current_diagnosis : 'unknown';
  const diagnostics = {
    icmp_normal:['Conectividade observada normal','Gateway e alvos externos respondem aos probes ICMP.','good','✓'],
    target_or_path_degraded:['Resposta externa parcial','Um ou mais alvos externos estão sem resposta. A causa ainda é indeterminada.','warning','!'],
    wan_unavailable_or_icmp_filtered:['Alvos externos sem resposta','O gateway responde; os alvos WAN não. Pode haver falha externa ou filtragem ICMP.','bad','!'],
    local_link_or_gateway_wan_unknown:['Gateway e alvos sem resposta','Há indício de problema local, no link ou no gateway. O estado WAN é indeterminado.','bad','!'],
    gateway_no_icmp_external_reachable:['Conexão externa observada','Há resposta externa apesar de o gateway não responder ao ICMP.','warning','!'],
    unknown:['Estado da conexão desconhecido',!latest ? 'Ainda não há amostras ICMP no banco.' : latest.clock_anomaly ? 'A amostra tem horário futuro. Verifique o relógio do sistema.' : latest.stale ? 'A coleta está desatualizada. O último estado não comprova a conexão atual.' : 'A última rodada não permite concluir o estado da conexão.','unknown','?']
  };
  const [title, description, kind, icon] = diagnostics[diagnosis] || diagnostics.unknown;
  $('health-banner').className = `health-banner ${kind}`;
  $('health-title').textContent = title;
  $('health-description').textContent = description;
  $('health-icon').textContent = icon;
  $('sample-age').textContent = latest ? `Amostra há ${duration(latest.age_seconds)}` : 'Sem amostras';
  const link = latest?.link;
  $('link-state').textContent = `${data.interface} · ${link?.carrier === true ? 'link ativo' : link?.carrier === false ? 'sem link físico' : 'link não informado'}`;
  $('speedtest-setting').textContent = data.speedtest_enabled ? 'Coleta de banda habilitada' : 'Coleta de banda desabilitada';
  $('timezone-label').textContent = `Horários em ${state.timezone} · armazenamento UTC`;
  const success = data.latest_successful_speedtest;
  const age = success ? (data.generated_us-success.timestamp_us)/1e6+(performance.now()-state.statusReceived)/1000 : null;
  $('speed-age').textContent = success ? (age < -1 ? 'Horário futuro' : `Há ${duration(Math.max(0,age))}`) : 'Sem medição';
  if (updatePanels) {
    const speedSignature = JSON.stringify([data.latest_speedtest,data.latest_successful_speedtest]);
    const dnsSignature = JSON.stringify(data.latest_dns);
    if (speedSignature !== state.speedSignature) {renderSpeedSummary(); state.speedSignature = speedSignature;}
    if (dnsSignature !== state.dnsSignature) {renderDNS(); state.dnsSignature = dnsSignature;}
  }
  updateDNSFreshness();
  document.querySelectorAll('[data-target-status]').forEach(el => {
    const sample = latest?.samples.find(item => item.target_id === el.dataset.targetStatus);
    el.innerHTML = outcome(!latest || latest.stale ? 'unknown' : sample?.status);
  });
}
function renderTargets() {
  const targets = Object.values(state.history.targets);
  $('target-cards').innerHTML = targets.length ? targets.map((target,index) => `<article class="panel target-card"><div class="target-heading"><span class="color-${index % 6}" aria-hidden="true">●</span><h3>${escapeText(target.name)}</h3><span data-target-status="${escapeText(target.id)}">${tag(target.role === 'gateway' ? 'Gateway' : 'WAN')}</span></div><div class="target-address">${escapeText(target.address)} · ${target.role === 'gateway' ? 'Rede local' : 'Alvo externo'}</div><div class="big-metric">${number(target.rtt_mean_ms)} <span>ms</span></div><div class="metric-label">Latência média no período · máx. ${number(target.rtt_max_ms)} ms</div><div class="metric-row"><div><strong>${percent(target.loss)}</strong><span>Perda</span></div><div><strong>${percent(target.availability)}</strong><span>Disponibilidade observada</span></div><div><strong>${number(target.jitter_ms)} ms</strong><span>Variação de RTT</span></div></div><div class="coverage-row"><span>Cobertura ${percent(target.coverage)}</span><span>${number(target.sent,0)} probes enviados</span></div><div class="coverage-track"><div class="coverage-fill" data-width="${target.coverage * 100}"></div></div><div class="cell-secondary">${number(target.operational_errors,0)} erros operacionais · execução ${percent(target.execution_coverage)}</div></article>`).join('') : empty('Nenhum alvo registrado.', 'Os alvos aparecerão após a primeira execução do coletor.');
  $('baseline-content').innerHTML = table(['Alvo','Perda total / sem carga','RTT médio sem carga','Variação sem carga','Cobertura sem carga'], targets.map(t => [escapeText(t.name),`${percent(t.loss)} / ${percent(t.without_speedtest.loss)}`,`${number(t.without_speedtest.rtt_mean_ms)} ms`,`${number(t.without_speedtest.jitter_ms)} ms`,`${percent(t.without_speedtest.coverage)}<div class="cell-secondary">${number(t.speedtest_samples,0)} amostras sob carga</div>`]));
  widths($('target-cards'));
}
function table(headers, rows, blank = 'Nenhum registro neste período.') {
  if (!rows.length) return empty(blank);
  return `<table class="data-table"><thead><tr>${headers.map(h => `<th scope="col">${escapeText(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${row.map((cell,i) => `<td data-label="${escapeText(headers[i])}">${cell}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
function chart(kind, key, unit) {
  const targets = Object.values(state.history.targets);
  const container = $(`${kind}-chart`);
  const inspector = $(`${kind}-selection`);
  $(`${kind}-legend`).innerHTML = targets.map((target,index) => `<span class="series color-${index % 6}">● ${escapeText(target.name)}</span>`).join('');
  if (!targets.length || !targets.some(target => target.buckets.some(bucket => Number.isFinite(bucket[key])))) {
    container.innerHTML = empty('Sem medições válidas no período.');
    container.className = 'chart-placeholder';
    inspector.innerHTML = '';
    return;
  }
  const width = 550, height = 220, left = 43, right = 12, top = 14, bottom = 30;
  const plotWidth = width-left-right, plotHeight = height-top-bottom;
  const buckets = targets[0].buckets;
  const maximum = Math.max(...targets.flatMap(target => target.buckets.map(bucket => Number.isFinite(bucket[key]) ? bucket[key] * (key === 'loss' ? 100 : 1) : 0)));
  const ceiling = maximum === 0 ? 1 : Math.ceil(maximum * 1.15 * (maximum < 5 ? 10 : 1)) / (maximum < 5 ? 10 : 1);
  const x = value => left + (value - state.history.start_us) / (state.history.end_us - state.history.start_us) * plotWidth;
  const y = value => top + plotHeight - value / ceiling * plotHeight;
  let svg = `<svg class="chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${kind === 'rtt' ? 'Latência' : 'Perda'} por alvo. Consulte os valores pelo controle abaixo.">`;
  for (let i = 0; i <= 4; i++) {
    const value = ceiling * i / 4;
    svg += `<line class="chart-grid" x1="${left}" y1="${y(value)}" x2="${width-right}" y2="${y(value)}"/><text x="${left-9}" y="${y(value)+4}" text-anchor="end">${escapeText(number(value,ceiling < 5 ? 1 : 0))}</text>`;
  }
  buckets.forEach((bucket,index) => {
    if (targets.some(target => (target.buckets[index]?.coverage ?? 0) < .99)) {
      svg += `<rect x="${x(bucket.start_us)}" y="${height-bottom+3}" width="${Math.max(1,x(bucket.end_us)-x(bucket.start_us))}" height="3" fill="#e1b66d" opacity="0.55"/>`;
    }
  });
  targets.forEach((target,index) => {
    let path = '', interrupted = true;
    target.buckets.forEach(bucket => {
      const raw = bucket[key];
      if (!Number.isFinite(raw)) { interrupted = true; return; }
      const value = raw * (key === 'loss' ? 100 : 1);
      const px = x((bucket.start_us+bucket.end_us)/2), py = y(value);
      path += `${interrupted ? 'M' : 'L'}${px.toFixed(2)},${py.toFixed(2)} `;
      if (interrupted) svg += `<circle class="color-${index % 6}" cx="${px}" cy="${py}" r="1.8" fill="currentColor"/>`;
      interrupted = false;
    });
    svg += `<path class="chart-line color-${index % 6}" d="${path}"/>`;
  });
  for (let i = 0; i <= 3; i++) {
    const stamp = state.history.start_us + (state.history.end_us-state.history.start_us) * i / 3;
    svg += `<text x="${x(stamp)}" y="${height-5}" text-anchor="${i === 0 ? 'start' : i === 3 ? 'end' : 'middle'}">${escapeText(state.window === '7d' ? date(stamp) : date(stamp,true))}</text>`;
  }
  container.className = '';
  container.innerHTML = svg + '</svg>';
  const inputId = `${kind}-range`;
  inspector.innerHTML = `<label for="${inputId}"><span>Consultar intervalo</span><span id="${kind}-interval"></span></label><input type="range" id="${inputId}" min="0" max="${buckets.length-1}" value="${buckets.length-1}" aria-label="Intervalo do gráfico de ${kind === 'rtt' ? 'latência' : 'perda'}"><div class="inspection-values" id="${kind}-values"></div>`;
  const inspect = index => {
    const bucket = buckets[index];
    $(`${kind}-interval`).textContent = `${date(bucket.start_us)} – ${date(bucket.end_us,true)}`;
    $(`${kind}-values`).innerHTML = targets.map((target,i) => {
      const value = target.buckets[index];
      return `<span class="color-${i % 6}">${escapeText(target.name)}: ${key === 'loss' ? percent(value.loss) : `${number(value[key])} ${unit}`} · cobertura ${percent(value.coverage)}${value.speedtest_samples ? ' · sob carga' : ''}</span>`;
    }).join('');
  };
  $(inputId).addEventListener('input', event => inspect(Number(event.target.value)));
  container.querySelector('svg').addEventListener('click', event => {
    const rect = event.currentTarget.getBoundingClientRect();
    const position = (event.clientX-rect.left) / rect.width * width;
    const index = Math.max(0,Math.min(buckets.length-1,Math.floor((position-left)/plotWidth*buckets.length)));
    $(inputId).value = index;
    inspect(index);
  });
  inspect(buckets.length-1);
}
function renderCharts() {
  chart('rtt',$('rtt-mode').value,'ms');
  chart('loss','loss','%');
}
function renderSpeedSummary() {
  if (!state.status) return;
  const success = state.status.latest_successful_speedtest;
  const attempt = state.status.latest_speedtest;
  const age = success ? Math.max(0,(state.status.generated_us-success.timestamp_us)/1e6+(performance.now()-state.statusReceived)/1000) : null;
  $('speed-age').textContent = success ? `Há ${duration(age)}` : 'Sem medição';
  const attemptHTML = `<div class="attempt">Última tentativa: ${outcome(attempt?.status)}${attempt ? `<span>${escapeText(date(attempt.timestamp_us))}</span>` : ''}</div>${attempt?.error ? `<details><summary>Detalhes da tentativa</summary><p>${escapeText(attempt.error)}</p></details>` : ''}`;
  if (!success) { $('speed-summary').innerHTML = empty('Nenhum teste bem-sucedido registrado.', 'Uma tentativa sem sucesso não representa velocidade zero.') + attemptHTML; return; }
  $('speed-summary').innerHTML = `<div class="speed-values">${['download','upload'].map(direction => `<div><div class="speed-direction ${direction}"><b aria-hidden="true">${direction === 'download' ? '↓' : '↑'}</b>${direction === 'download' ? 'Download' : 'Upload'}</div><div class="big-metric">${number(success[direction+'_mbps'],1)} <span>Mb/s</span></div><div class="progress-track"><div class="progress-fill ${direction === 'upload' ? 'upload-fill' : ''}" data-width="${success[direction+'_plan_percent'] ?? 0}"></div></div><div class="plan-label">${speedPercent(success[direction+'_plan_percent'])} · ${number(success['plan_'+direction+'_mbps'],0)} Mb/s</div></div>`).join('')}</div><div class="speed-details"><span>${escapeText(date(success.timestamp_us))}</span><span>Servidor: ${escapeText(success.server.name || success.server.host || 'não informado')}</span><span>IP observado: ${escapeText(success.public_ip || 'não informado')}</span>${success.public_ip_matches_expected !== null ? `<span>${success.public_ip_matches_expected ? 'IP coincide com o esperado' : 'IP difere do esperado'}</span>` : '<span>Comparação de IP não configurada</span>'}</div><p class="footnote">Resultado pontual; não representa a velocidade atual da conexão.</p>${attemptHTML}`;
  widths($('speed-summary'));
}
function renderSpeedChart() {
  const items = state.history.speedtests.results.slice(-18);
  const root = $('speed-chart');
  if (!items.length) {root.innerHTML = empty('Nenhum teste neste período.'); return;}
  const max = Math.max(1,...items.flatMap(item => [item.download_mbps || 0,item.upload_mbps || 0])) * 1.15;
  root.className = '';
  root.innerHTML = `<div class="speed-bars">${items.map((item,index) => `<div class="speed-bar-group ${item.status !== 'success' ? 'failed' : ''}">${item.status === 'success' ? `<div class="speed-bar" data-height="${item.download_mbps/max*100}"></div><div class="speed-bar upload" data-height="${item.upload_mbps/max*100}"></div>` : '<span>×</span>'}<span class="speed-bar-label">${items.length <= 8 || index % Math.ceil(items.length/6) === 0 ? escapeText(date(item.timestamp_us,true)) : ''}</span><button data-speed-index="${index}" aria-label="Consultar teste de ${escapeText(date(item.timestamp_us))}"></button></div>`).join('')}</div><div class="speed-selected" id="speed-selected"></div>`;
  const show = index => {
    const item = items[index];
    $('speed-selected').innerHTML = `${escapeText(date(item.timestamp_us))} · ${outcome(item.status)} · ↓ ${number(item.download_mbps)} / ↑ ${number(item.upload_mbps)} Mb/s${state.history.speedtests.results.length > 18 ? ' · gráfico limitado aos últimos 18 testes carregados' : ''}`;
  };
  root.querySelectorAll('[data-speed-index]').forEach(el => el.addEventListener('click', () => show(Number(el.dataset.speedIndex))));
  widths(root);
  show(items.length-1);
}
function renderSpeedTable() {
  const data = state.history.speedtests;
  $('speed-count').textContent = `${data.successful} de ${data.samples} testes bem-sucedidos`;
  $('speed-table').innerHTML = table(['Data / resultado','Download','Upload','Servidor / detalhes'], data.results.slice().reverse().map(item => [ `${escapeText(date(item.timestamp_us))}<div class="cell-secondary">${outcome(item.status)}</div>`, `${number(item.download_mbps)} Mb/s<div class="cell-secondary">${speedPercent(item.download_plan_percent)}</div>`, `${number(item.upload_mbps)} Mb/s<div class="cell-secondary">${speedPercent(item.upload_plan_percent)}</div>`, `${escapeText(item.server.name || item.server.host || '—')}<details><summary>Ver detalhes</summary><p>IP: ${escapeText(item.public_ip || '—')} · interface ${escapeText(item.interface)}<br>Duração: ${duration(item.duration_seconds)} · latência CLI ${number(item.latency_ms)} ms<br>Transferido: ${number(item.download_bytes === null && item.upload_bytes === null ? null : ((item.download_bytes || 0)+(item.upload_bytes || 0))/1e9,2)} GB${item.error ? `<br>${escapeText(item.error)}` : ''}</p></details>` ]));
  $('more-speedtests').hidden = data.next_cursor === null;
}
function updateDNSFreshness() {
  if (!state.status) return;
  Object.entries(state.status.latest_dns).forEach(([id, raw]) => {
    const item = current(raw,state.status.dns_interval_seconds);
    const card = Array.from(document.querySelectorAll('[data-dns-id]')).find(el => el.dataset.dnsId === id);
    if (!card) return;
    card.querySelector('[data-dns-status]').innerHTML = outcome(!item || item.stale ? 'unknown' : item.current_status);
    card.querySelector('[data-dns-age]').textContent = !item ? 'sem amostras' : item.clock_anomaly ? 'horário futuro' : `há ${duration(item.age_seconds)}`;
  });
}
function renderDNS() {
  const historical = state.history?.resolvers || {};
  const latest = state.status?.latest_dns || {};
  const ids = [...new Set([...Object.keys(historical),...Object.keys(latest)])];
  $('dns-cards').innerHTML = ids.length ? ids.map(id => {
    const metrics = historical[id];
    const raw = latest[id];
    const item = state.status ? current(raw,state.history?.intervals?.dns_seconds || state.status.dns_interval_seconds || 30) : null;
    const status = !item || item.stale ? 'unknown' : item.current_status;
    return `<article class="panel dns-card" data-dns-id="${escapeText(id)}"><div class="panel-heading"><h3>${id === 'system' ? 'Resolver do sistema' : escapeText(id)}</h3><span data-dns-status>${outcome(status)}</span></div><div class="dns-current">${item ? `${escapeText(item.name)} · ${escapeText(item.path)} · ` : 'Sem observações recentes · '}<span data-dns-age>${item ? `há ${duration(item.age_seconds)}` : 'sem amostras'}</span></div><div class="metric-row"><div><strong>${percent(metrics?.query_availability)}</strong><span>Consultas válidas</span></div><div><strong>${number(metrics?.duration_mean_ms)} ms</strong><span>Duração média</span></div><div><strong>${percent(metrics?.coverage)}</strong><span>Cobertura</span></div></div><div class="cell-secondary">${metrics ? Object.entries(metrics.counts).map(([key,count]) => `${escapeText(labels[key] || key)}: ${number(count,0)}`).join(' · ') || 'Sem consultas no período' : 'Histórico ainda não carregado'}</div>${item?.error ? `<details><summary>Detalhes da última consulta</summary><p>${escapeText(item.error)}</p></details>` : ''}</article>`;
  }).join('') : empty('Nenhum resolver registrado.');
}
function scopeName(scope) {
  if (scope === 'wan') return 'Alvos WAN';
  if (scope === 'local') return 'Rede local / gateway';
  if (scope.startsWith('target:')) {const id = scope.slice(7); return state.history.targets[id]?.name || id;}
  if (scope.startsWith('dns:')) return `DNS · ${scope.slice(4)}`;
  return scope;
}
function renderIncidents() {
  const items = state.history.incidents;
  $('incident-count').textContent = `${state.history.incidents_total} ${state.history.incidents_total === 1 ? 'incidente' : 'incidentes'} no período`;
  $('incident-table').innerHTML = table(['Escopo / estado','Início / fim','Duração observada','Qualidade / detalhes'], items.slice().reverse().map(item => [ `${escapeText(scopeName(item.scope))}<div class="cell-secondary">${tag(item.open ? 'Aberto' : 'Encerrado',item.open ? 'warning' : 'neutral')}</div>`, `${escapeText(date(item.started_us))}<div class="cell-secondary">${item.open ? 'Sem fim confirmado' : escapeText(date(item.ended_us))}</div>`, `${duration(item.observed_seconds_in_window)}<div class="cell-secondary">na janela selecionada</div>`, `${tag(item.quality === 'continuous' ? 'Contínua' : 'Com lacunas',item.quality === 'continuous' ? 'neutral' : 'warning')}<details><summary>Ver detalhes</summary><p>Confirmação: ${escapeText(date(item.confirmed_us))}<br>Candidato de recuperação: ${escapeText(date(item.recovery_us))}<br>Total observado: ${duration(item.observed_seconds)}<br>Intervalo civil na janela: ${duration(item.civil_span_seconds)}<br>Lacunas: ${number(item.gap_count,0)}. O intervalo civil não representa queda comprovada.</p></details>` ]), 'Nenhum incidente confirmado neste período.');
  $('more-incidents').hidden = state.history.incidents_next_id === null;
}
function renderGaps() {
  const names = {restart:'Reinício do coletor',missed:'Rodadas não executadas',late:'Atraso de execução',schedule_skip:'Rodadas puladas',schedule_delay:'Atraso de execução'};
  $('gap-count').textContent = `${state.history.gaps_total} registros`;
  $('gap-table').innerHTML = table(['Data','Tipo','Duração registrada','Detalhes'], state.history.gaps.slice().reverse().map(item => [escapeText(date(item.timestamp_us)),escapeText(names[item.kind] || item.kind),duration(item.duration_seconds),escapeText(item.details || '—')]), 'Nenhuma lacuna registrada nesta janela.');
  $('more-gaps').hidden = state.history.gaps_next_id === null;
}
function renderCoverage() {
  const data = state.history;
  const coverage = data.coverage;
  const messages = [];
  if (coverage.history_started_us > data.start_us) messages.push(`A janela começa antes do início do histórico (${date(coverage.history_started_us)}). A cobertura inclui esse período sem observação.`);
  if (coverage.data_removed) messages.push('Parte dos dados desta janela já foi removida pela retenção.');
  if (!Object.values(data.targets).some(t => t.samples)) messages.push('Não há amostras ICMP nesta janela. Não é possível avaliar sua disponibilidade.');
  $('coverage-note').hidden = !messages.length;
  $('coverage-note').textContent = messages.join(' ');
}
function renderHistory() {
  $('history-time').textContent = `Últimas ${{'1h':'1 hora','24h':'24 horas','7d':'7 dias'}[state.history.window]} · ${date(state.history.start_us)} a ${date(state.history.end_us)} · atualização a cada 60 s`;
  renderTargets(); renderCharts(); renderSpeedChart(); renderSpeedTable(); renderDNS(); renderIncidents(); renderGaps(); renderCoverage(); renderStatus();
}
async function loadStatus() {
  if (state.statusBusy) return;
  state.statusBusy = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(),8000);
  try {
    state.status = await request('/api/status',controller.signal);
    state.statusReceived = performance.now();
    state.timezone = state.status.timezone;
    reportError('status',null);
    renderStatus();
  } catch (error) { if (!state.status) {$('health-banner').className = 'health-banner unknown'; $('health-title').textContent = 'Dados atuais indisponíveis'; $('health-description').textContent = 'Não foi possível obter as últimas observações do coletor.'; $('health-icon').textContent = '?';} reportError('status',`Estado atual: ${error.name === 'AbortError' ? 'a consulta excedeu o tempo de espera.' : error.message} ${state.status ? 'A idade das últimas amostras continua avançando.' : ''}`); }
  finally {clearTimeout(timeout); state.statusBusy = false;}
}
async function loadHistory(force = false) {
  if (state.historyController && !force) return;
  state.historyController?.abort();
  const sequence = ++state.historySequence;
  const controller = new AbortController();
  state.historyController = controller;
  const timeout = setTimeout(() => controller.abort(),25000);
  document.body.classList.add('history-loading');
  try {
    const data = await request(`/api/history?window=${state.window}`,controller.signal);
    if (sequence !== state.historySequence) return;
    state.history = data;
    state.timezone = data.timezone;
    state.paging = {};
    reportError('history',null);
    renderHistory();
  } catch (error) {
    if (sequence !== state.historySequence) return;
    reportError('history',`Histórico: ${error.name === 'AbortError' ? 'a consulta excedeu o tempo de espera.' : error.message}${state.history ? ' A tela mantém o período e o horário da última consulta concluída.' : ''}`);
    if (!state.history) {
      $('history-time').textContent = 'Histórico indisponível';
      $('target-cards').innerHTML = empty('Histórico indisponível.','Verifique a mensagem acima e tente atualizar.');
      ['rtt-chart','loss-chart','speed-chart','speed-table','incident-table','gap-table'].forEach(id => {$(id).innerHTML = empty('Sem dados disponíveis.');});
    }
  } finally {
    clearTimeout(timeout);
    if (sequence === state.historySequence) {state.historyController = null; document.body.classList.remove('history-loading');}
  }
}
async function loadMore(kind) {
  if (!state.history || state.paging[kind]) return;
  const history = state.history;
  const cursor = kind === 'speedtests' ? history.speedtests.next_cursor : history[`${kind}_next_id`];
  if (cursor === null) return;
  const button = $(`more-${kind}`);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(),25000);
  state.paging[kind] = true; button.disabled = true;
  try {
    const page = await request(`/api/${kind}?start_us=${history.start_us}&end_us=${history.end_us}&cursor=${cursor}&limit=1000`,controller.signal);
    if (history !== state.history) return;
    if (kind === 'speedtests') {history.speedtests.results.push(...page.results); history.speedtests.next_cursor = page.next_cursor; renderSpeedTable(); renderSpeedChart();}
    else {history[kind].push(...page.items); history[`${kind}_next_id`] = page.next_cursor; kind === 'incidents' ? renderIncidents() : renderGaps();}
    reportError(`page-${kind}`,null);
  } catch (error) {reportError(`page-${kind}`,error.name === 'AbortError' ? 'A consulta da próxima página excedeu o tempo de espera.' : error.message);}
  finally {clearTimeout(timeout); state.paging[kind] = false; button.disabled = false;}
}

document.querySelectorAll('[data-window]').forEach(button => button.addEventListener('click',() => {
  state.window = button.dataset.window;
  document.querySelectorAll('[data-window]').forEach(item => {item.classList.toggle('selected',item === button); item.setAttribute('aria-pressed',String(item === button));});
  loadHistory(true);
}));
$('rtt-mode').addEventListener('change',() => {if (state.history) chart('rtt',$('rtt-mode').value,'ms');});
$('refresh').addEventListener('click',async () => { $('refresh').disabled = true; await Promise.allSettled([loadStatus(),loadHistory(true)]); $('refresh').disabled = false; });
['incidents','gaps','speedtests'].forEach(kind => $(`more-${kind}`).addEventListener('click',() => loadMore(kind)));
document.querySelectorAll('nav a').forEach(link => link.addEventListener('click',() => {document.querySelectorAll('nav a').forEach(item => item.classList.toggle('active',item === link));}));
document.addEventListener('visibilitychange',() => {if (!document.hidden) {loadStatus(); loadHistory();}});
setInterval(() => {if (!document.hidden) loadStatus();},5000);
setInterval(() => {if (!document.hidden && !Object.values(state.paging).some(Boolean)) loadHistory();},60000);
setInterval(() => {if (!document.hidden) renderStatus(false);},1000);
loadStatus();
loadHistory();

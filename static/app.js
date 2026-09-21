const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];

const COLORS = ["#267fd1","#20a06f","#ef9b45","#7d74df","#25a5ad","#d26f91","#7297b6","#8cb64d","#b27a51","#40a0df","#6d86c5","#46aa82","#d7805d","#8c79ad"];
const state = {
  fuels: {
    ai92:"АИ-92", ai95:"АИ-95", ai95_1:"АИ-95+",
    dt:"ДТ", dt_1:"ДТ+", ai98:"АИ-98", ai100:"АИ-100"
  },
  stations: [],
  date: "live",
  fuel: "ai92",
  owner: "",
  drilledOwner: "",
  search: "",
  availableOnly: false,
  fetchedAt: null,
  stale: false,
  historyCache: new Map(),
  loading: true
};

let toastTimer = null;

function esc(value){
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }[ch]));
}

function fmtDate(value){
  if(!value) return "—";
  const d = new Date(value.length === 10 ? value + "T00:00:00+03:00" : value);
  if(Number.isNaN(d.getTime())) return value;
  return new Intl.DateTimeFormat("ru-RU",{day:"2-digit",month:"2-digit",year:"numeric"}).format(d);
}

function fmtDateTime(value){
  if(!value) return "—";
  const d = new Date(value);
  if(Number.isNaN(d.getTime())) return value;
  return new Intl.DateTimeFormat("ru-RU",{
    day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit"
  }).format(d);
}

function showToast(message, error=false){
  const t = $("#toast");
  t.textContent = message;
  t.classList.toggle("error", error);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>t.classList.remove("show"), 3400);
}

async function api(url, options={}){
  const response = await fetch(url,{cache:"no-store",...options});
  let body = null;
  try{ body = await response.json(); }catch(_){}
  if(!response.ok){
    const detail = body?.detail || `${response.status} ${response.statusText}`;
    throw new Error(detail);
  }
  return body;
}

function currentOwner(){
  return state.drilledOwner || state.owner;
}

function stationAvailable(station){
  return !!station.fuels?.[state.fuel];
}

function filteredStations(){
  let rows = [...state.stations];
  const owner = currentOwner();
  if(owner) rows = rows.filter(s => s.owner === owner);
  if(state.search){
    const q = state.search.toLocaleLowerCase("ru");
    rows = rows.filter(s => `${s.name} ${s.address} ${s.owner}`.toLocaleLowerCase("ru").includes(q));
  }
  if(state.availableOnly) rows = rows.filter(stationAvailable);
  return rows;
}

function overviewStations(){
  let rows = [...state.stations];
  if(state.owner) rows = rows.filter(s => s.owner === state.owner);
  if(state.search){
    const q = state.search.toLocaleLowerCase("ru");
    rows = rows.filter(s => `${s.name} ${s.address} ${s.owner}`.toLocaleLowerCase("ru").includes(q));
  }
  return rows;
}

function ownerGroups(){
  const map = new Map();
  for(const station of overviewStations()){
    if(!map.has(station.owner)) map.set(station.owner,{name:station.owner,total:0,available:0});
    const group = map.get(station.owner);
    group.total += 1;
    if(stationAvailable(station)) group.available += 1;
  }
  return [...map.values()].sort((a,b)=> b.available-a.available || b.total-a.total || a.name.localeCompare(b.name,"ru"));
}

function polar(cx,cy,r,angle){
  const rad=(angle-90)*Math.PI/180;
  return {x:cx+r*Math.cos(rad), y:cy+r*Math.sin(rad)};
}

function arc(cx,cy,r,a0,a1){
  const start=polar(cx,cy,r,a1), end=polar(cx,cy,r,a0);
  const large=(a1-a0)<=180?0:1;
  return `M ${start.x} ${start.y} A ${r} ${r} 0 ${large} 0 ${end.x} ${end.y}`;
}

function renderKpis(){
  const rows = filteredStations();
  const available = rows.filter(stationAvailable).length;
  $("#kpiAvailable").textContent = available;
  $("#kpiTotal").textContent = rows.length;
  $("#kpiCoverage").textContent = rows.length ? Math.round(available/rows.length*100)+"%" : "0%";

  if(state.date === "live"){
    $("#kpiUpdated").textContent = state.fetchedAt ? fmtDateTime(state.fetchedAt) : "—";
    $("#kpiUpdatedNote").textContent = state.stale ? "показан последний успешный кэш" : "последний успешный запрос";
    $("#kpiAvailableNote").textContent = `топливо: ${state.fuels[state.fuel]}`;
  }else{
    $("#kpiUpdated").textContent = fmtDate(state.date);
    $("#kpiUpdatedNote").textContent = "архивный срез";
    $("#kpiAvailableNote").textContent = `архив · ${state.fuels[state.fuel]}`;
  }
}

function renderOwners(){
  const groups = ownerGroups();
  $("#ownerCount").textContent = groups.length;
  const totalAvailable = groups.reduce((sum,g)=>sum+g.available,0);
  $("#donutCenter").textContent = totalAvailable;

  const svg = $("#donut");
  svg.innerHTML = "";
  $("#donutEmpty").classList.toggle("show", totalAvailable === 0);

  if(totalAvailable > 0){
    let angle = 0;
    groups.filter(g=>g.available>0).forEach((g,index)=>{
      const fraction = g.available/totalAvailable;
      const next = angle + fraction*360;
      const path = document.createElementNS("http://www.w3.org/2000/svg","path");
      path.setAttribute("d", arc(220,170,103,angle,next));
      path.setAttribute("fill","none");
      path.setAttribute("stroke", COLORS[index%COLORS.length]);
      path.setAttribute("stroke-width","50");
      path.style.cursor="pointer";
      path.style.transition="opacity .18s, filter .18s";
      path.addEventListener("mouseenter",()=>{path.style.filter="brightness(1.08)";});
      path.addEventListener("mouseleave",()=>{path.style.filter="";});
      path.addEventListener("click",()=>drill(g.name));
      svg.appendChild(path);
      angle = next;
    });

    const ring = document.createElementNS("http://www.w3.org/2000/svg","circle");
    ring.setAttribute("cx","220"); ring.setAttribute("cy","170"); ring.setAttribute("r","77");
    ring.setAttribute("fill","#fbfdff"); ring.setAttribute("stroke","#e2edf5");
    svg.appendChild(ring);
  }

  $("#ownerList").innerHTML = groups.map((g,index)=>`
    <button class="owner-row ${state.drilledOwner===g.name?"active":""}" type="button" data-owner="${esc(g.name)}">
      <span class="owner-dot" style="background:${COLORS[index%COLORS.length]}"></span>
      <span>
        <span class="owner-name">${esc(g.name)}</span>
        <span class="owner-meta">${g.available} с ${esc(state.fuels[state.fuel])} из ${g.total}</span>
      </span>
      <span class="owner-val">${g.total ? Math.round(g.available/g.total*100) : 0}%</span>
    </button>`).join("");

  $$(".owner-row").forEach(btn=>btn.addEventListener("click",()=>drill(btn.dataset.owner)));
}

function drill(owner){
  state.drilledOwner = owner;
  $("#stationPanel").classList.add("drilled");
  renderAll();
  $("#stationPanel").scrollIntoView({behavior:"smooth",block:"start"});
}

function resetDrill(){
  state.drilledOwner = "";
  $("#stationPanel").classList.remove("drilled");
  renderAll();
}

function openOnMap(station){
  const frame = $("#mapFrame");
  const url = station.portal_url || `https://azs.geoportal40.ru/#${station.longitude}_${station.latitude}_17`;
  $("#mapLoader").classList.remove("hidden");
  frame.src = url;
  $("#mapExternal").href = url;
  $("#mapSelectionName").textContent = station.name;
  $("#mapSelectionAddress").textContent = station.address;
  $("#mapSelection").classList.add("show");
  $("#mapSubtitle").textContent = "Выбрана станция из текущего списка";
  $(".map-panel").scrollIntoView({behavior:"smooth",block:"center"});
}

function resetMap(){
  $("#mapLoader").classList.remove("hidden");
  $("#mapFrame").src = "https://azs.geoportal40.ru/";
  $("#mapExternal").href = "https://azs.geoportal40.ru/";
  $("#mapSelection").classList.remove("show");
  $("#mapSelectionName").textContent = "АЗС не выбрана";
  $("#mapSelectionAddress").textContent = "Нажмите «На карте» в списке";
  $("#mapSubtitle").textContent = "Официальная карта Geoportal40";
}

function historyMarkup(payload, fuel){
  const rows = payload.history || [];
  const cells = rows.map(day=>{
    let cls = "history-cell";
    let title = `${fmtDate(day.date)}: данных нет`;
    if(day.known && day.fuels){
      const present = !!day.fuels[fuel];
      cls += present ? " present" : " absent";
      title = `${fmtDate(day.date)}: ${present ? "топливо было" : "топлива не было"}; получено ${fmtDateTime(day.fetched_at)}`;
      if(day.changes) title += `; изменений внутри дня: ${day.changes}`;
    }
    return `<button class="${cls}" type="button" data-note="${esc(title)}" title="${esc(title)}"></button>`;
  }).join("");

  const labels = rows.map((day,index)=>{
    const date = new Date(day.date+"T00:00:00+03:00");
    const label = index%3===0 ? date.getDate() : "";
    return `<span>${label}</span>`;
  }).join("");

  return `
    <div class="history-scroll">
      <div class="history-grid">${cells}</div>
      <div class="history-labels">${labels}</div>
    </div>
    <div class="history-legend">
      <span class="legend-item"><i class="legend-dot present"></i>Есть</span>
      <span class="legend-item"><i class="legend-dot absent"></i>Нет</span>
      <span class="legend-item"><i class="legend-dot"></i>Архив ещё не накоплен</span>
    </div>
    <div class="history-note">Нажмите на день для подробностей.</div>`;
}

async function toggleHistory(row, station){
  const box = $(".history", row);
  const button = $(".history-btn", row);
  const opening = !box.classList.contains("show");

  if(!opening){
    box.classList.remove("show");
    button.classList.remove("history-open");
    button.textContent = "История";
    return;
  }

  box.classList.add("show");
  button.classList.add("history-open");
  button.textContent = "Скрыть";
  const body = $(".history-body", box);
  body.innerHTML = `<div class="history-loading">Получаем архив…</div>`;

  try{
    let payload = state.historyCache.get(String(station.id));
    if(!payload){
      payload = await api(`/api/history/${encodeURIComponent(station.id)}?days=30`);
      state.historyCache.set(String(station.id), payload);
    }
    body.innerHTML = historyMarkup(payload, state.fuel);
    $$(".history-cell", body).forEach(cell=>{
      cell.addEventListener("click",()=>{
        $(".history-note", body).textContent = cell.dataset.note || "—";
      });
    });
  }catch(error){
    body.innerHTML = `<div class="history-note">Не удалось получить историю: ${esc(error.message)}</div>`;
  }
}

function renderStations(){
  const rows = filteredStations();
  $("#visibleCount").textContent = rows.length;
  $("#stationLoading").classList.toggle("hidden", !state.loading);
  $("#emptyState").classList.toggle("show", !state.loading && rows.length===0);
  $("#stationList").style.display = rows.length ? "flex" : "none";

  const owner = currentOwner();
  $("#stationTitle").textContent = owner || "Все АЗС";
  $("#stationSubtitle").textContent = `${state.fuels[state.fuel]} · ${state.date==="live" ? "актуальный срез" : "срез "+fmtDate(state.date)}`;

  $("#stationList").innerHTML = rows.map((station,index)=>{
    const available = stationAvailable(station);
    return `
      <article class="station-item ${available?"":"unavailable"}" data-id="${station.id}">
        <div class="station-index">${index+1}</div>
        <div class="station-main">
          <div class="station-name">${esc(station.name)}</div>
          <div class="station-address">${esc(station.address || "Адрес не указан")}</div>
          <div class="station-update">${available ? state.fuels[state.fuel]+" есть" : state.fuels[state.fuel]+" нет"} · данные ${fmtDateTime(station.fetched_at)}</div>
        </div>
        <div class="station-actions">
          <button class="station-action map-btn" type="button">На карте</button>
          <button class="station-action history-btn" type="button">История</button>
        </div>
        <div class="history">
          <div class="history-head">
            <strong>История за 30 дней · ${esc(state.fuels[state.fuel])}</strong>
            <span>${esc(station.owner)}</span>
          </div>
          <div class="history-body"></div>
        </div>
      </article>`;
  }).join("");

  $$(".station-item").forEach((row,index)=>{
    const station = rows[index];
    $(".map-btn", row).addEventListener("click",()=>openOnMap(station));
    $(".history-btn", row).addEventListener("click",()=>toggleHistory(row,station));
  });
}

function renderModes(){
  $("#modeAll").classList.toggle("active", !state.availableOnly);
  $("#modeAvailable").classList.toggle("active", state.availableOnly);
}

function renderAll(){
  renderKpis();
  renderOwners();
  renderStations();
  renderModes();
  $("#stationPanel").classList.toggle("drilled", !!state.drilledOwner);
}

function setSourceStatus({ok, stale=false, error=null, fetchedAt=null}){
  const dot = $("#sourceDot");
  dot.className = "source-dot";
  if(ok && !stale){
    dot.classList.add("ok");
    $("#sourceState").textContent = "Geoportal40 подключён";
    $("#sourceMeta").textContent = fetchedAt ? `Получено ${fmtDateTime(fetchedAt)}` : "Актуальные данные";
    $("#footerState").textContent = "Архив истории ведётся локально приложением";
  }else if(ok && stale){
    dot.classList.add("bad");
    $("#sourceState").textContent = "Показан последний успешный кэш";
    $("#sourceMeta").textContent = error || (fetchedAt ? `Последний срез ${fmtDateTime(fetchedAt)}` : "Geoportal временно недоступен");
    $("#footerState").textContent = "Geoportal временно недоступен · показан кэш";
  }else{
    dot.classList.add("bad");
    $("#sourceState").textContent = "Нет данных Geoportal40";
    $("#sourceMeta").textContent = error || "Проверьте доступ к порталу";
    $("#footerState").textContent = "Нет успешного среза";
  }
}

function rebuildOwnerFilter(){
  const current = state.owner;
  const owners = [...new Set(state.stations.map(s=>s.owner))].sort((a,b)=>a.localeCompare(b,"ru"));
  $("#ownerFilter").innerHTML = `<option value="">Все владельцы</option>` + owners.map(owner=>`<option value="${esc(owner)}">${esc(owner)}</option>`).join("");
  if(owners.includes(current)) $("#ownerFilter").value = current;
  else { state.owner=""; $("#ownerFilter").value=""; }
}

async function loadMeta(){
  const meta = await api("/api/meta");
  if(meta.fuels) state.fuels = meta.fuels;

  $("#fuelFilter").innerHTML = Object.entries(state.fuels).map(([key,label])=>`<option value="${key}">${esc(label)}</option>`).join("");
  $("#fuelFilter").value = state.fuel;

  const dates = meta.dates || [];
  $("#dateFilter").innerHTML = `<option value="live">Сейчас · Geoportal40</option>` +
    dates.map(date=>`<option value="${date}">${fmtDate(date)}</option>`).join("");
  $("#dateFilter").value = state.date;

  const success = meta.last_success;
  const refresh = meta.refresh || {};
  setSourceStatus({
    ok: !!success,
    stale: success ? !refresh.ok : false,
    error: refresh.error || meta.last_failure?.error || null,
    fetchedAt: success?.fetched_at || null
  });
}

async function loadStations({force=false}={}){
  state.loading = true;
  renderStations();
  const url = state.date === "live"
    ? `/api/stations${force ? "?force=true" : ""}`
    : `/api/stations?date=${encodeURIComponent(state.date)}`;

  try{
    const payload = await api(url);
    state.stations = payload.stations || [];
    state.fetchedAt = payload.fetched_at || null;
    state.stale = !!payload.stale;
    state.loading = false;
    state.historyCache.clear();
    rebuildOwnerFilter();

    if(payload.mode === "live"){
      setSourceStatus({
        ok: state.stations.length > 0,
        stale: state.stale,
        error: payload.refresh_error,
        fetchedAt: state.fetchedAt
      });
    }else{
      $("#sourceState").textContent = `Архивный срез ${fmtDate(state.date)}`;
      $("#sourceMeta").textContent = `${state.stations.length} АЗС`;
      $("#sourceDot").className = "source-dot ok";
      $("#footerState").textContent = "Показан локальный архив, накопленный приложением";
    }

    renderAll();
  }catch(error){
    state.loading = false;
    state.stations = [];
    renderAll();
    setSourceStatus({ok:false,error:error.message});
    showToast(error.message,true);
  }
}

async function forceRefresh(){
  if(state.date !== "live"){
    state.date = "live";
    $("#dateFilter").value = "live";
  }
  const btn = $("#refreshBtn");
  btn.disabled = true;
  btn.textContent = "Обновление…";
  try{
    await api("/api/refresh",{method:"POST"});
    await loadMeta();
    await loadStations({force:false});
    showToast("Данные Geoportal40 обновлены");
  }catch(error){
    showToast(error.message,true);
    await loadStations({force:false});
  }finally{
    btn.disabled = false;
    btn.textContent = "Обновить";
  }
}

function resetFilters(){
  state.owner = "";
  state.drilledOwner = "";
  state.search = "";
  state.availableOnly = false;
  $("#ownerFilter").value = "";
  $("#searchFilter").value = "";
  resetDrill();
}

function bind(){
  $("#fuelFilter").addEventListener("change",event=>{
    state.fuel = event.target.value;
    renderAll();
  });
  $("#dateFilter").addEventListener("change",async event=>{
    state.date = event.target.value;
    state.drilledOwner = "";
    await loadStations();
  });
  $("#ownerFilter").addEventListener("change",event=>{
    state.owner = event.target.value;
    state.drilledOwner = "";
    renderAll();
  });
  $("#searchFilter").addEventListener("input",event=>{
    state.search = event.target.value.trim();
    renderAll();
  });
  $("#modeAll").addEventListener("click",()=>{
    state.availableOnly = false;
    renderAll();
  });
  $("#modeAvailable").addEventListener("click",()=>{
    state.availableOnly = true;
    renderAll();
  });
  $("#backBtn").addEventListener("click",resetDrill);
  $("#resetBtn").addEventListener("click",resetFilters);
  $("#refreshBtn").addEventListener("click",forceRefresh);
  $("#mapReset").addEventListener("click",resetMap);
  $("#mapFrame").addEventListener("load",()=>$("#mapLoader").classList.add("hidden"));
}

async function init(){
  bind();
  try{
    await loadMeta();
  }catch(error){
    showToast("Не удалось получить метаданные: "+error.message,true);
  }
  await loadStations();
}

init();

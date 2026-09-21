const $ = s => document.querySelector(s);

let challengeId = "";
let expiresAt = 0;
let countdownTimer = null;
let pollTimer = null;
let polling = false;

function setNotice(message, ok=false){
  const box = $("#notice");
  if(!message){
    box.hidden = true;
    box.textContent = "";
    box.classList.remove("ok");
    return;
  }
  box.hidden = false;
  box.textContent = message;
  box.classList.toggle("ok", ok);
}

async function api(url, body){
  const response = await fetch(url,{
    method:"POST",
    headers:{"Content-Type":"application/json"},
    cache:"no-store",
    credentials:"same-origin",
    body:JSON.stringify(body || {})
  });
  let payload = {};
  try{ payload = await response.json(); }catch(_){}
  if(!response.ok){
    throw new Error(payload.detail || "Ошибка авторизации");
  }
  return payload;
}

function stopWaiting(){
  clearInterval(countdownTimer);
  clearInterval(pollTimer);
  countdownTimer = null;
  pollTimer = null;
  polling = false;
}

function showPassword(){
  stopWaiting();
  challengeId = "";
  $("#approvalPanel").hidden = true;
  $("#passwordForm").hidden = false;
  $("#password").value = "";
  $("#password").focus();
}

function updateCountdown(){
  const left = Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000));
  if(left <= 0){
    $("#countdown").textContent = "Запрос истёк. Введи пароль ещё раз.";
    stopWaiting();
    setTimeout(showPassword, 900);
    return;
  }
  const m = Math.floor(left / 60);
  const s = String(left % 60).padStart(2,"0");
  $("#countdown").textContent = `Запрос действует ещё ${m}:${s}`;
}

async function pollApproval(){
  if(!challengeId || polling) return;
  polling = true;
  try{
    const result = await api("/api/auth/poll",{challenge_id:challengeId});
    if(result.status === "approved"){
      stopWaiting();
      setNotice("Вход подтверждён. Открываю панель…", true);
      window.location.replace("/");
      return;
    }
    if(result.status === "denied"){
      stopWaiting();
      setNotice("Вход отклонён в Telegram.");
      setTimeout(showPassword, 1000);
      return;
    }
    if(result.status === "expired" || result.status === "consumed"){
      stopWaiting();
      setNotice("Запрос на вход больше не действует.");
      setTimeout(showPassword, 1000);
    }
  }catch(error){
    setNotice(error.message);
  }finally{
    polling = false;
  }
}

$("#passwordForm").addEventListener("submit", async event=>{
  event.preventDefault();
  setNotice("");

  const btn = $("#passwordBtn");
  btn.disabled = true;
  btn.textContent = "Проверяем…";

  try{
    const result = await api("/api/auth/password",{password:$("#password").value});
    challengeId = result.challenge_id;
    expiresAt = Date.now() + (result.expires_in || 180) * 1000;

    $("#passwordForm").hidden = true;
    $("#approvalPanel").hidden = false;

    setNotice(
      result.reused
        ? "Запрос уже отправлен. Подтверди его в Telegram."
        : "Запрос отправлен в Telegram.",
      true
    );

    updateCountdown();
    stopWaiting();
    countdownTimer = setInterval(updateCountdown,1000);
    pollTimer = setInterval(pollApproval,1400);
    pollApproval();
  }catch(error){
    setNotice(error.message);
  }finally{
    btn.disabled = false;
    btn.textContent = "Продолжить";
  }
});

$("#backBtn").addEventListener("click",()=>{
  setNotice("");
  showPassword();
});

(async()=>{
  try{
    const response = await fetch("/api/auth/status",{
      cache:"no-store",
      credentials:"same-origin"
    });
    const status = await response.json();

    if(status.authenticated){
      window.location.replace("/");
      return;
    }

    if(!status.configured){
      setNotice("Защита установлена, но ещё не настроена на сервере.");
      $("#passwordBtn").disabled = true;
      return;
    }

    if(!status.telegram_approval_ready){
      setNotice("HTTPS/Telegram-подтверждение ещё не настроено на сервере.");
      $("#passwordBtn").disabled = true;
    }
  }catch(_){}
})();

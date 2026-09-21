
const $ = s => document.querySelector(s);
let challengeId = "";
let expiresAt = 0;
let timer = null;

function setNotice(message, ok=false){
  const box = $("#notice");
  if(!message){
    box.hidden = true;
    box.textContent = "";
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
    body:JSON.stringify(body || {})
  });
  let payload = {};
  try{payload = await response.json()}catch(_){}
  if(!response.ok){
    throw new Error(payload.detail || "Ошибка авторизации");
  }
  return payload;
}

function showPassword(){
  challengeId = "";
  clearInterval(timer);
  $("#codeForm").hidden = true;
  $("#passwordForm").hidden = false;
  $("#code").value = "";
  $("#password").focus();
}

function updateCountdown(){
  const left = Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000));
  if(left <= 0){
    $("#countdown").textContent = "Код истёк. Вернитесь и запросите новый.";
    clearInterval(timer);
    return;
  }
  const m = Math.floor(left / 60);
  const s = String(left % 60).padStart(2,"0");
  $("#countdown").textContent = `Код действует ещё ${m}:${s}`;
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
    $("#codeForm").hidden = false;
    $("#code").focus();
    updateCountdown();
    clearInterval(timer);
    timer = setInterval(updateCountdown,1000);
    setNotice("Пароль принят. Код отправлен в Telegram.", true);
  }catch(error){
    setNotice(error.message);
  }finally{
    btn.disabled = false;
    btn.textContent = "Продолжить";
  }
});

$("#codeForm").addEventListener("submit", async event=>{
  event.preventDefault();
  setNotice("");
  const code = $("#code").value.replace(/\D/g,"").slice(0,6);
  if(code.length !== 6){
    setNotice("Введите 6 цифр из Telegram");
    return;
  }
  const btn = $("#verifyBtn");
  btn.disabled = true;
  btn.textContent = "Входим…";
  try{
    await api("/api/auth/verify",{challenge_id:challengeId,code});
    window.location.replace("/");
  }catch(error){
    setNotice(error.message);
    $("#code").select();
  }finally{
    btn.disabled = false;
    btn.textContent = "Войти";
  }
});

$("#code").addEventListener("input",event=>{
  event.target.value = event.target.value.replace(/\D/g,"").slice(0,6);
});

$("#backBtn").addEventListener("click",()=>{
  setNotice("");
  showPassword();
});

(async()=>{
  try{
    const response = await fetch("/api/auth/status",{cache:"no-store"});
    const status = await response.json();
    if(status.authenticated){
      window.location.replace("/");
      return;
    }
    if(!status.configured){
      setNotice("Защита установлена, но ещё не настроена на сервере.");
      $("#passwordBtn").disabled = true;
    }
  }catch(_){}
})();

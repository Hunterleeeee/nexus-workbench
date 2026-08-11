const learnQuery = (selector, root = document) => root.querySelector(selector);
const learnEscape = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
const learningState = { dashboard: null, draftTimer: 0, draftRevision: 0, draftPromise: null };

function learningSetStatus(message = "", tone = "") {
  const node = learnQuery("#learning-page-status");
  if (!node) return;
  node.textContent = message;
  node.className = `learning-page-status${tone ? ` ${tone}` : ""}`;
  node.setAttribute("role", tone === "error" ? "alert" : "status");
}

function learningBusy(button, busy, label = "处理中…") {
  if (!button) return;
  if (busy) {
    button.dataset.idleText = button.textContent;
    button.textContent = label;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  } else {
    button.textContent = button.dataset.idleText || button.textContent;
    delete button.dataset.idleText;
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

function renderLearningStats(stats = {}) {
  learnQuery("#stat-streak").textContent = stats.streak ?? 0;
  learnQuery("#stat-completed").textContent = stats.completed ?? 0;
  learnQuery("#stat-weekly").textContent = stats.weekly_completed ?? 0;
  learnQuery("#stat-accuracy").textContent = stats.quiz_accuracy ?? 0;
}

function lessonSourceLabel(lesson = {}) {
  return lesson.source === "personalized" ? "按目标生成" : "内置课程";
}

function lessonFeedbackMarkup(lesson, quiz) {
  if (!lesson.completed) return '<div id="quiz-feedback" class="quiz-feedback" role="status" aria-live="polite"></div>';
  const correct = Boolean(lesson.quiz_correct);
  return `<div id="quiz-feedback" class="quiz-feedback visible ${correct ? "correct" : "review"}" role="status"><strong>${correct ? "回答正确" : "这题值得再看一遍"}</strong><br>${learnEscape(quiz.explanation || "课程已完成，重点是把方法用进自己的工作。")}</div>`;
}

function renderTodayLesson(lesson = {}) {
  window.clearTimeout(learningState.draftTimer);
  learningState.draftTimer = 0;
  learningState.draftRevision += 1;
  const host = learnQuery("#today-lesson");
  const content = lesson.content || {};
  const caseItem = content.case || {};
  const practice = content.practice || {};
  const quiz = content.quiz || {};
  const knowledge = Array.isArray(content.knowledge) ? content.knowledge : [];
  const steps = Array.isArray(practice.steps) ? practice.steps : [];
  const completed = Boolean(lesson.completed);
  const selectedAnswer = Number(lesson.quiz_answer);
  host.innerHTML = `
    <header class="lesson-head">
      <div>
        <div class="lesson-meta">
          <span class="lesson-chip">第 ${learnEscape(lesson.day_index || 1)} 课</span>
          <span class="lesson-chip">${learnEscape(lesson.module || "AI 转型")}</span>
          <span class="lesson-chip source">${learnEscape(lessonSourceLabel(lesson))}</span>
          ${completed ? '<span class="lesson-chip complete">今日已完成</span>' : ""}
        </div>
        <h2 id="lesson-title">${learnEscape(lesson.title || content.title || "今日 AI 转型课")}</h2>
        <p class="lesson-objective">${learnEscape(content.objective || "今天完成一个知识、案例、练习与复盘闭环。")}</p>
      </div>
      <div class="lesson-actions">
        <button id="regenerate-lesson" class="secondary-button" type="button" ${completed ? "disabled" : ""} title="按当前学习设置重新生成课程"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M19 8a7 7 0 1 0 1 5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M19 4v4h-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>换一节</button>
        <button id="save-lesson-note" class="secondary-button" type="button" ${!completed || lesson.note_artifact_id ? "disabled" : ""} title="${completed ? "保存课程和学习记录" : "完成课程后可保存"}"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 4.5h12v15H6z" stroke="currentColor" stroke-width="1.5"/><path d="M9 8h6M9 11h6M9 14h4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>${lesson.note_artifact_id ? "已存笔记" : completed ? "存为笔记" : "完成后存笔记"}</button>
      </div>
    </header>
    <div class="lesson-body">
      <section class="lesson-section" aria-labelledby="knowledge-title">
        <div class="lesson-section-head"><span class="lesson-step">1</span><div><h3 id="knowledge-title">知识</h3><p>理解今天要用的方法</p></div></div>
        <ol class="knowledge-list">${knowledge.map((item) => `<li>${learnEscape(item)}</li>`).join("")}</ol>
      </section>
      <section class="lesson-section" aria-labelledby="case-title">
        <div class="lesson-section-head"><span class="lesson-step">2</span><div><h3 id="case-title">工作案例</h3><p>查看方法在工作中的用法</p></div></div>
        <div class="case-panel"><div class="case-flow">
          <div class="case-item"><span>场景</span><p>${learnEscape(caseItem.situation || "")}</p></div>
          <div class="case-item"><span>做法</span><p>${learnEscape(caseItem.approach || "")}</p></div>
          <div class="case-item"><span>结果</span><p>${learnEscape(caseItem.result || "")}</p></div>
          <div class="case-item"><span>经验</span><p>${learnEscape(caseItem.lesson || "")}</p></div>
        </div></div>
      </section>
      <section class="lesson-section" aria-labelledby="practice-title">
        <div class="lesson-section-head"><span class="lesson-step">3</span><div><h3 id="practice-title">练习</h3><p>按步骤完成并记录结果</p></div></div>
        <div class="practice-box"><div><h4>${learnEscape(practice.task || "把今天的方法用到一项真实工作中。")}</h4><ol class="practice-steps">${steps.map((step) => `<li>${learnEscape(step)}</li>`).join("")}</ol></div><div class="deliverable-card"><span>交付物</span><strong>${learnEscape(practice.deliverable || "一条可复用的方法记录")}</strong></div></div>
        <label class="practice-output-field" for="practice-output"><span class="practice-output-head"><span>练习成果 <small>必填</small></span><span id="draft-status" class="draft-status ${completed || lesson.status === "in_progress" ? "saved" : ""}" role="status" aria-live="polite">${completed ? "已完成" : lesson.status === "in_progress" ? "已保存" : "自动保存"}</span></span><textarea id="practice-output" rows="5" maxlength="8000" placeholder="粘贴结果、写下方案，或记录你实际完成了什么" ${completed ? "readonly" : ""}>${learnEscape(lesson.practice_output || "")}</textarea></label>
      </section>
      <section class="lesson-section" aria-labelledby="quiz-title">
        <div class="lesson-section-head"><span class="lesson-step">4</span><div><h3 id="quiz-title">自测与复盘</h3><p>选择答案并记录复盘</p></div></div>
        <form id="lesson-complete-form" class="quiz-form" data-lesson-id="${learnEscape(lesson.id)}">
          <p class="quiz-question">${learnEscape(quiz.question || "今天最重要的一点是什么？")}</p>
          <div class="quiz-options" role="radiogroup" aria-label="选择答案">${(quiz.options || []).map((option, index) => `<label class="quiz-option"><input type="radio" name="quiz_answer" value="${index}" ${selectedAnswer === index ? "checked" : ""} ${completed ? "disabled" : ""} /><span>${learnEscape(option)}</span></label>`).join("")}</div>
          ${completed ? `<div class="lesson-complete-summary"><strong>今日课程已完成</strong><p>${learnEscape(lesson.reflection || "没有填写复盘。")}</p></div>` : `<div class="reflection-grid"><label for="lesson-reflection">复盘<textarea id="lesson-reflection" rows="3" maxlength="4000" placeholder="这节课对你的工作有什么用？">${learnEscape(lesson.reflection || "")}</textarea></label><label class="confidence-field" for="lesson-confidence">掌握程度<select id="lesson-confidence">${[1, 2, 3, 4, 5].map((value) => `<option value="${value}" ${Number(lesson.confidence || 3) === value ? "selected" : ""}>${value} · ${["还没懂", "有点模糊", "基本理解", "能用起来", "能教别人"][value - 1]}</option>`).join("")}</select></label></div>`}
          ${lessonFeedbackMarkup(lesson, quiz)}
          <div class="quiz-actions"><span class="takeaway"><strong>本课要点：</strong>${learnEscape(content.takeaway || "把方法用到真实任务里。")}</span>${completed ? "" : '<button class="primary-button" type="submit">完成今日学习</button>'}</div>
        </form>
      </section>
    </div>`;
  const sourceNode = learnQuery("#learning-source-status");
  sourceNode.textContent = completed ? "今日课程已完成" : `${lessonSourceLabel(lesson)} · 约 ${learningState.dashboard?.profile?.daily_minutes || 25} 分钟`;
  sourceNode.className = "learning-status-chip";
  if (lesson.generation_warning) learningSetStatus(lesson.generation_warning);
  bindLessonActions();
}

function renderLearningProfile(profile = {}) {
  learnQuery("#current-role").value = profile.current_role || "";
  learnQuery("#target-role").value = profile.target_role || "";
  learnQuery("#experience").value = profile.experience || "beginner";
  learnQuery("#focus").value = profile.focus || "work-efficiency";
  learnQuery("#learning-goal").value = profile.goal || "";
  learnQuery("#daily-minutes").value = String(profile.daily_minutes || 25);
  const summary = learnQuery("#profile-summary");
  if (profile.current_role || profile.target_role) {
    summary.textContent = `${profile.current_role || "当前岗位"} → ${profile.target_role || "目标岗位"} · 每天 ${profile.daily_minutes || 25} 分钟`;
  } else {
    summary.textContent = "填写岗位和学习目标后，课程会优先使用相关工作场景。";
    learnQuery("#profile-form").classList.remove("hidden");
    learnQuery("#profile-toggle").setAttribute("aria-expanded", "true");
  }
}

function renderLearningPush(dashboard = {}) {
  const profile = dashboard.profile || {};
  const rule = dashboard.automation || {};
  const push = dashboard.push || {};
  learnQuery("#push-enabled").checked = Boolean(profile.daily_push_enabled);
  learnQuery("#push-time").value = profile.push_time || "08:30";
  learnQuery("#push-rule-note").textContent = profile.daily_push_enabled ? `每天 ${profile.push_time || "08:30"} · 本地时间` : "已暂停";
  learnQuery("#push-dot").classList.toggle("ready", Boolean(profile.daily_push_enabled));
  const status = learnQuery("#learning-push-status");
  status.className = `learning-status-chip ${push.ready ? "" : "neutral"}`.trim();
  status.textContent = profile.daily_push_enabled ? `${rule.enabled ? "每日提醒已开启" : "提醒规则待启用"}${push.ready ? " · 浏览器已订阅" : " · 仅工作台内提醒"}` : "每日提醒已暂停";
  learnQuery("#subscribe-push").textContent = push.ready ? "管理当前设备 Push" : "订阅浏览器 Push";
}

function renderLearningPhases(phases = [], today = {}) {
  const activeIndex = Math.min(phases.length - 1, Math.max(0, Math.floor((Number(today.day_index || 1) - 1) / 4)));
  learnQuery("#learning-phases").innerHTML = phases.map((phase, index) => `<li class="learning-phase ${index === activeIndex ? "active" : ""}"><span class="phase-index">${index + 1}</span><div><strong>${learnEscape(phase.title)}</strong><small>${learnEscape(phase.description)}</small><span>${learnEscape(phase.days)}</span></div></li>`).join("");
}

function renderLessonHistory(items = []) {
  const host = learnQuery("#lesson-history");
  if (!items.length) {
    host.innerHTML = '<div class="history-empty">完成第一节课后，这里会留下学习记录。</div>';
    return;
  }
  host.innerHTML = items.slice(0, 8).map((item) => `<article class="history-item ${item.completed ? "completed" : item.status === "in_progress" ? "in-progress" : ""}"><span class="history-state" aria-hidden="true"></span><div class="history-copy"><strong>${learnEscape(item.title)}</strong><small>${learnEscape(item.module || "AI 转型")} · ${learnEscape(item.lesson_date)}</small></div><span>${item.completed ? (item.quiz_correct ? "答对" : "已学") : item.status === "in_progress" ? "进行中" : "待完成"}</span></article>`).join("");
}

function renderLearningDashboard(dashboard) {
  learningState.dashboard = dashboard;
  renderLearningStats(dashboard.stats || {});
  renderLearningProfile(dashboard.profile || {});
  renderLearningPush(dashboard);
  renderLearningPhases(dashboard.phases || [], dashboard.today || {});
  renderLessonHistory(dashboard.history || []);
  renderTodayLesson(dashboard.today || {});
}

async function loadLearningDashboard() {
  learningSetStatus("");
  try {
    const dashboard = await requestJson("/api/ai-learning/dashboard");
    renderLearningDashboard(dashboard);
  } catch (error) {
    learningSetStatus(`读取学习项目失败：${error.message}`, "error");
    learnQuery("#today-lesson").innerHTML = `<div class="lesson-loading" role="alert"><strong>今日课程暂时没有读取成功</strong><p>${learnEscape(error.message)}</p><button id="retry-learning" class="secondary-button" type="button">重新加载</button></div>`;
    learnQuery("#retry-learning")?.addEventListener("click", loadLearningDashboard);
  }
}

async function regenerateTodayLesson(button) {
  if (hasCurrentLessonDraft() && !window.confirm("这节课已有练习或复盘内容。换课后会清空，确定继续吗？")) return;
  window.clearTimeout(learningState.draftTimer);
  learningState.draftTimer = 0;
  learningState.draftRevision += 1;
  if (learningState.draftPromise) {
    try { await learningState.draftPromise; } catch (_error) { /* confirmed replacement can continue */ }
  }
  learningBusy(button, true, "生成中…");
  learningSetStatus("正在重新生成今日课程…");
  try {
    const body = await requestJson("/api/ai-learning/lessons/today/generate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh: true }) });
    learningState.dashboard.today = body.lesson;
    renderTodayLesson(body.lesson);
    learningSetStatus(body.lesson.generation_warning || (body.lesson.source === "personalized" ? "今日课程已按学习设置重新生成。" : "已换为内置课程。配置全局 LLM 后可按学习设置生成。"));
  } catch (error) {
    learningSetStatus(`重新生成失败：${error.message}`, "error");
  } finally {
    learningBusy(button, false);
  }
}

async function saveLessonNote(button, lessonId) {
  learningBusy(button, true, "保存中…");
  try {
    const body = await requestJson(`/api/ai-learning/lessons/${encodeURIComponent(lessonId)}/note`, { method: "POST" });
    learningSetStatus(body.created ? "课程和学习记录已保存到知识库。" : "这节课已经保存过了。", "");
    if (body.lesson) {
      learningState.dashboard.today = body.lesson;
      renderTodayLesson(body.lesson);
    }
  } catch (error) {
    learningSetStatus(`保存笔记失败：${error.message}`, "error");
  } finally {
    learningBusy(button, false);
  }
}

function currentLessonDraftPayload() {
  return {
    practice_output: learnQuery("#practice-output")?.value.trim() || "",
    reflection: learnQuery("#lesson-reflection")?.value.trim() || "",
    confidence: Number(learnQuery("#lesson-confidence")?.value || 3),
  };
}

function hasCurrentLessonDraft() {
  const lesson = learningState.dashboard?.today || {};
  const draft = currentLessonDraftPayload();
  return lesson.status === "in_progress" || Boolean(draft.practice_output || draft.reflection);
}

function setLessonDraftStatus(message, tone = "") {
  const node = learnQuery("#draft-status");
  if (!node) return;
  node.textContent = message;
  node.className = `draft-status${tone ? ` ${tone}` : ""}`;
}

async function saveCurrentLessonDraft() {
  const lesson = learningState.dashboard?.today || {};
  if (!lesson.id || lesson.completed) return null;
  window.clearTimeout(learningState.draftTimer);
  learningState.draftTimer = 0;
  const revision = learningState.draftRevision;
  const payload = currentLessonDraftPayload();
  setLessonDraftStatus("保存中…", "saving");
  const previous = learningState.draftPromise;
  const operation = (previous ? previous.catch(() => {}) : Promise.resolve()).then(() => requestJson(`/api/ai-learning/lessons/${encodeURIComponent(lesson.id)}/progress`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }));
  learningState.draftPromise = operation;
  try {
    const body = await operation;
    if (body.lesson && learningState.dashboard?.today?.id === body.lesson.id) {
      learningState.dashboard.today = body.lesson;
      const index = (learningState.dashboard.history || []).findIndex((item) => item.id === body.lesson.id);
      if (index >= 0) learningState.dashboard.history[index] = body.lesson;
      renderLessonHistory(learningState.dashboard.history || []);
    }
    if (revision === learningState.draftRevision) setLessonDraftStatus("已保存", "saved");
    return body;
  } catch (error) {
    if (revision === learningState.draftRevision) setLessonDraftStatus(`保存失败：${error.message}`, "error");
    throw error;
  } finally {
    if (learningState.draftPromise === operation) learningState.draftPromise = null;
  }
}

function scheduleLessonDraftSave() {
  learningState.draftRevision += 1;
  setLessonDraftStatus("未保存");
  window.clearTimeout(learningState.draftTimer);
  learningState.draftTimer = window.setTimeout(() => saveCurrentLessonDraft().catch(() => {}), 700);
}

function bindLessonActions() {
  const lesson = learningState.dashboard?.today || {};
  learnQuery("#regenerate-lesson")?.addEventListener("click", (event) => regenerateTodayLesson(event.currentTarget));
  learnQuery("#save-lesson-note")?.addEventListener("click", (event) => saveLessonNote(event.currentTarget, lesson.id));
  [learnQuery("#practice-output"), learnQuery("#lesson-reflection")].forEach((field) => field?.addEventListener("input", scheduleLessonDraftSave));
  learnQuery("#lesson-confidence")?.addEventListener("change", scheduleLessonDraftSave);
  learnQuery("#lesson-complete-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const draft = currentLessonDraftPayload();
    if (!draft.practice_output) {
      const feedback = learnQuery("#quiz-feedback");
      feedback.className = "quiz-feedback visible review";
      feedback.textContent = "请先填写练习成果。";
      learnQuery("#practice-output")?.focus();
      return;
    }
    const selected = form.querySelector('input[name="quiz_answer"]:checked');
    if (!selected) {
      const feedback = learnQuery("#quiz-feedback");
      feedback.className = "quiz-feedback visible review";
      feedback.textContent = "先选择一个答案，再完成今天的学习。";
      form.querySelector('input[name="quiz_answer"]')?.focus();
      return;
    }
    const button = event.submitter || form.querySelector('button[type="submit"]');
    window.clearTimeout(learningState.draftTimer);
    learningState.draftTimer = 0;
    learningState.draftRevision += 1;
    learningBusy(button, true, "记录中…");
    try {
      const body = await requestJson(`/api/ai-learning/lessons/${encodeURIComponent(form.dataset.lessonId)}/complete`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ quiz_answer: Number(selected.value), ...draft }) });
      learningSetStatus(body.quiz.correct ? "回答正确，今天的学习已记录。" : "今天的学习已记录；答案解释已经展开，建议再读一遍。", "");
      await loadLearningDashboard();
      learnQuery("#today-lesson")?.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      learningSetStatus(`记录学习失败：${error.message}`, "error");
    } finally {
      learningBusy(button, false);
    }
  });
}

function currentProfilePayload(overrides = {}) {
  const current = learningState.dashboard?.profile || {};
  return {
    current_role: learnQuery("#current-role")?.value.trim() ?? current.current_role ?? "",
    target_role: learnQuery("#target-role")?.value.trim() ?? current.target_role ?? "",
    experience: learnQuery("#experience")?.value || current.experience || "beginner",
    focus: learnQuery("#focus")?.value || current.focus || "work-efficiency",
    goal: learnQuery("#learning-goal")?.value.trim() ?? current.goal ?? "",
    daily_minutes: Number(learnQuery("#daily-minutes")?.value || current.daily_minutes || 25),
    push_time: learnQuery("#push-time")?.value || current.push_time || "08:30",
    daily_push_enabled: learnQuery("#push-enabled")?.checked ?? Boolean(current.daily_push_enabled),
    ...overrides,
  };
}

function setupLearningProfile() {
  learnQuery("#profile-toggle")?.addEventListener("click", (event) => {
    const form = learnQuery("#profile-form");
    const open = form.classList.toggle("hidden") === false;
    event.currentTarget.setAttribute("aria-expanded", String(open));
    if (open) learnQuery("#current-role")?.focus();
  });
  learnQuery("#profile-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    const message = learnQuery("#profile-message");
    learningBusy(button, true, "保存中…");
    message.textContent = "正在保存学习设置…";
    try {
      const body = await requestJson("/api/ai-learning/profile", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(currentProfilePayload()) });
      learningState.dashboard.profile = body.profile;
      learningState.dashboard.automation = body.automation;
      renderLearningProfile(body.profile);
      renderLearningPush(learningState.dashboard);
      message.textContent = "学习设置已保存。点击“换一节”后，新课程会使用这些设置。";
    } catch (error) {
      message.textContent = `保存失败：${error.message}`;
    } finally {
      learningBusy(button, false);
    }
  });
}

function urlBase64ToBytes(value) {
  const padding = "=".repeat((4 - value.length % 4) % 4);
  const base64 = (value + padding).replaceAll("-", "+").replaceAll("_", "/");
  return Uint8Array.from(atob(base64), (character) => character.charCodeAt(0));
}

async function ensureLearningServiceWorker() {
  if (!("serviceWorker" in navigator)) throw new Error("当前浏览器不支持 Service Worker");
  await navigator.serviceWorker.register("/static/sw.js?v=0.3.153", { scope: "/" });
  return navigator.serviceWorker.ready;
}

async function subscribeLearningPush(button) {
  learningBusy(button, true, "订阅中…");
  const message = learnQuery("#push-message");
  try {
    if (!("PushManager" in window) || !("Notification" in window)) throw new Error("当前浏览器不支持 Web Push");
    const config = await requestJson("/api/push/config");
    if (!config.public_key) throw new Error("服务端尚未配置 VAPID 公钥，目前只能收到工作台内提醒");
    const permission = await Notification.requestPermission();
    if (permission !== "granted") throw new Error("浏览器通知权限未授权");
    const registration = await ensureLearningServiceWorker();
    const existing = await registration.pushManager.getSubscription();
    const subscription = existing || await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToBytes(config.public_key) });
    const json = subscription.toJSON();
    await requestJson("/api/push/subscriptions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys || {}, user_agent: navigator.userAgent, quiet_start: "22:00", quiet_end: "07:00", enabled: true }) });
    learningState.dashboard.push = { ...(learningState.dashboard.push || {}), ready: true, subscriptions: Math.max(1, Number(learningState.dashboard.push?.subscriptions || 0)) };
    renderLearningPush(learningState.dashboard);
    message.textContent = "当前浏览器已订阅每日学习提醒。";
  } catch (error) {
    message.textContent = error.message;
  } finally {
    learningBusy(button, false);
  }
}

function setupLearningPush() {
  ensureLearningServiceWorker().catch(() => {});
  learnQuery("#push-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    const message = learnQuery("#push-message");
    learningBusy(button, true, "保存中…");
    try {
      const body = await requestJson("/api/ai-learning/profile", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(currentProfilePayload()) });
      learningState.dashboard.profile = body.profile;
      learningState.dashboard.automation = body.automation;
      renderLearningPush(learningState.dashboard);
      message.textContent = body.profile.daily_push_enabled ? `已设置为每天 ${body.profile.push_time} 提醒。` : "每日学习提醒已暂停。";
    } catch (error) {
      message.textContent = `保存失败：${error.message}`;
    } finally {
      learningBusy(button, false);
    }
  });
  learnQuery("#subscribe-push")?.addEventListener("click", (event) => subscribeLearningPush(event.currentTarget));
}

function setupAILearning() {
  setupLearningProfile();
  setupLearningPush();
  loadLearningDashboard();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setupAILearning);
else setupAILearning();
